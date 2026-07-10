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
import types

import isar.drive.loop as loop_mod
from isar.drive.loop import (_campaign_config, _errors_arg_for, _master_obs_from_row,
                             _observation_from_row, _pick_interval, _resume_state,
                             _run_nonce, run_campaign)
from isar.drive.objective import ObjectiveSpec
from isar.drive.session_log import log_event, read_log
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


def make_mock_runner(omega_true, *, window_gate=True, hesse_only=False, blank_members=False):
    """A fake `isar run`: writes results.csv from the saved scans, including the master (pooled) row
    per (Q,T) that ISAR now emits. The master's interval shrinks ~1/√N with the pooled production
    count, so a q converges by accumulation. Declines when the window misses the peak.

    ``hesse_only=True`` makes production + master rows carry a *trusted HESSE* interval
    (``hesse_lo/hi`` + ``hesse_trusted``) with the bootstrap ``interval_lo/hi`` empty — the fast-tier
    path, so the loop converges on HESSE and then runs its certification pass."""
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
                             "confidence": 0.9, "reasons": "resolved"})
                if hesse_only and n > QUICKLOOK_NCOUNT:
                    base.update({"hesse_lo": true - raw_hw, "hesse_hi": true + raw_hw,
                                 "hesse_trusted": True})
                else:
                    base.update({"interval_lo": true - raw_hw, "interval_hi": true + raw_hw})
                if n > QUICKLOOK_NCOUNT:
                    prod_per_h[h] = prod_per_h.get(h, 0) + 1
            rows.append(base)
        # Master (pooled) rows: one per H with pooled production scans; interval ~ 0.3/√N.
        master_scan = 9_000_000
        for h, npool in sorted(prod_per_h.items()):
            hw = 0.3 / _math.sqrt(npool)
            true = omega_true(h)
            mrow = {"scan": master_scan, "detector": "det", "h": h, "k": 0.0, "l": 0.0,
                    "status": "fitted", "verdict": "resolved", "disposition": "resolved",
                    "omega0": true, "interval_reliable": True, "pooled": True, "n_members": npool,
                    # Chain of custody: member labels (";"-joined). blank_members leaves the cell
                    # empty while n_members>0 to exercise the summary's evidence-chain guard.
                    "members": ("" if blank_members else ";".join(str(i) for i in range(npool))),
                    "confidence": 0.9, "reasons": "pooled master"}
            if hesse_only:
                mrow.update({"hesse_lo": true - hw, "hesse_hi": true + hw, "hesse_trusted": True})
            else:
                mrow.update({"interval_lo": true - hw, "interval_hi": true + hw})
            rows.append(mrow)
            master_scan += 1
        report = Path(workspace) / "report"
        report.mkdir(parents=True, exist_ok=True)
        cols = ["scan", "detector", "h", "k", "l", "status", "verdict", "disposition",
                "confidence", "omega0", "omega0_err", "interval_lo", "interval_hi",
                "interval_reliable", "hesse_lo", "hesse_hi", "hesse_trusted",
                "pooled", "n_members", "members", "reasons"]
        with open(report / "results.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c) for c in cols})
    return runner


# --------------------------------------------------------------------------- S3 certifiability
def _urow(**kw):
    base = {"disposition": "resolved", "omega0": 5.0, "h": 2.1, "k": 0.0, "l": 0.0}
    base.update(kw)
    return base


def test_observation_reliable_bootstrap_is_certifiable():
    o = _observation_from_row(
        _urow(interval_lo=4.9, interval_hi=5.1, interval_reliable=True),
        "production", PRODUCTION_NCOUNT, _UNITY_CAL)
    assert o["certifiable"] is True and o["uncertifiable_kind"] is None


def test_observation_gamma_at_bound_is_uncertifiable():
    # interval_reliable=False with a profile fallback: _pick_interval says ok, but the flag makes it
    # uncertifiable — it must not seat convergence (the mid-zone hole).
    o = _observation_from_row(
        _urow(disposition="flagged", interval_lo=4.999, interval_hi=5.001, interval_reliable=False,
              omega0_profile_lo=4.99, omega0_profile_hi=5.01),
        "production", PRODUCTION_NCOUNT, _UNITY_CAL)
    assert o["resolved"] is True and o["certifiable"] is False
    assert o["uncertifiable_kind"] == "gamma_at_bound"


def test_observation_trusted_hesse_is_certifiable():
    # The fast-tier carve-out: interval_reliable=False but a TRUSTED HESSE interval still certifies.
    o = _observation_from_row(
        _urow(interval_reliable=False, hesse_lo=4.95, hesse_hi=5.05, hesse_trusted=True),
        "production", PRODUCTION_NCOUNT, _UNITY_CAL)
    assert o["certifiable"] is True and o["uncertifiable_kind"] is None


def test_observation_untrusted_fast_tier_is_uncertifiable():
    o = _observation_from_row(
        _urow(interval_reliable=False, hesse_lo=4.95, hesse_hi=5.05, hesse_trusted=False),
        "production", PRODUCTION_NCOUNT, _UNITY_CAL)
    assert o["certifiable"] is False and o["uncertifiable_kind"] == "fast_tier"


def test_master_gamma_at_bound_is_uncertifiable():
    m = _master_obs_from_row(
        _urow(disposition="flagged", interval_lo=4.999, interval_hi=5.001, interval_reliable=False,
              omega0_profile_lo=4.99, omega0_profile_hi=5.01),
        _UNITY_CAL)
    assert m["reliable"] is True and m["certifiable"] is False


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


def test_pick_interval_uses_trusted_hesse_when_no_bootstrap():
    # Fast tier: only the HESSE interval is present and trusted → use it.
    row = {"interval_lo": None, "interval_hi": None, "interval_reliable": None,
           "omega0_profile_lo": None, "omega0_profile_hi": None,
           "hesse_lo": 4.95, "hesse_hi": 5.05, "hesse_trusted": True}
    assert _pick_interval(row) == (4.95, 5.05, True)


def test_pick_interval_untrusted_hesse_is_unusable():
    # Fast tier, untrusted (Γ-at-bound / low counts): not usable → escalate to full, don't certify.
    row = {"interval_lo": None, "interval_hi": None, "interval_reliable": None,
           "hesse_lo": 4.95, "hesse_hi": 5.05, "hesse_trusted": False}
    assert _pick_interval(row) == (None, None, False)


def test_pick_interval_bootstrap_outranks_hesse():
    # Full tier present alongside HESSE → bootstrap wins (most authoritative).
    row = {"interval_lo": 4.9, "interval_hi": 5.1, "interval_reliable": True,
           "hesse_lo": 4.95, "hesse_hi": 5.05, "hesse_trusted": True}
    assert _pick_interval(row) == (4.9, 5.1, True)


# --------------------------------------------------------------------------- error-tier ladder
_HCOLS = ["scan", "detector", "h", "k", "l", "status", "verdict", "disposition", "omega0",
          "interval_lo", "interval_hi", "interval_reliable", "omega0_profile_lo",
          "omega0_profile_hi", "hesse_lo", "hesse_hi", "hesse_trusted", "pooled", "n_members",
          "reasons"]


def _write_results(ws, rows):
    report = ws / "report"
    report.mkdir(parents=True, exist_ok=True)
    with open(report / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_HCOLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) for c in _HCOLS})


def _prop(h, kind="production"):
    nc = QUICKLOOK_NCOUNT if kind == "quicklook" else PRODUCTION_NCOUNT
    return types.SimpleNamespace(h=h, k=0.0, l=0.0, kind=kind, ncount=nc)


def test_errors_tier_ladder_fast_by_default_escalates_on_untrusted_hesse(tmp_path):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    # Quick-looks are always cheap (steer only).
    assert _errors_arg_for(_prop(2.1, "quicklook"), ws) == ["--errors", "cheap"]
    # No prior fit at this q → production runs fast.
    assert _errors_arg_for(_prop(2.1), ws) == ["--errors", "fast"]
    # A usable fit whose only error is an untrusted HESSE → escalate to full (needs the reliable interval).
    _write_results(ws, [{"scan": 1, "detector": "det", "h": 2.1, "k": 0.0, "l": 0.0,
                         "status": "fitted", "disposition": "flagged", "omega0": 0.95,
                         "hesse_lo": 0.94, "hesse_hi": 0.96, "hesse_trusted": False}])
    assert _errors_arg_for(_prop(2.1), ws) == ["--errors", "full"]
    # A trusted-HESSE q stays on the cheap fast tier.
    _write_results(ws, [{"scan": 2, "detector": "det", "h": 2.2, "k": 0.0, "l": 0.0,
                         "status": "fitted", "disposition": "resolved", "omega0": 1.1,
                         "hesse_lo": 1.09, "hesse_hi": 1.11, "hesse_trusted": True}])
    assert _errors_arg_for(_prop(2.2), ws) == ["--errors", "fast"]


def test_campaign_reaches_met_via_hesse_then_certifies(tmp_path):
    # Production/master fits carry only a trusted HESSE interval (fast tier); the campaign converges
    # on it and then runs its bootstrap certification pass (logged).
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client, make_mock_runner(omega, hesse_only=True))
    assert summary["stop"]["state"] == "met", summary["stop"]["reasons"]
    assert all(g["converged"] for g in summary["grid"])
    events = [(e["kind"], (e.get("payload") or {}).get("event")) for e in read_log(summary["session_log"])]
    assert ("decide", "certify_start") in events
    assert ("decide", "certify_done") in events


def _objective(tol=0.5, amount=1e13):
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=tol,
                                 budget={"kind": "neutrons", "amount": amount}, name="mvp")


def _run(tmp_path, objective, client, runner, **extra):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True, exist_ok=True)
    cfg = {"workspace": ws, "client": client, "run_isar": runner, "calibration": _UNITY_CAL,
           "resolution": {"fwhm_dE": 0.8}, "seed": 7, **extra}
    return run_campaign(objective, cfg), ws


def test_campaign_summary_carries_member_provenance(tmp_path):
    # Chain of custody: every pooled dispersion + grid entry names its members, len == n_members.
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client, make_mock_runner(omega))
    pooled_disp = [d for d in summary["dispersion"] if d.get("n_members")]
    assert pooled_disp
    for d in pooled_disp:
        assert d["members"] and len(d["members"]) == d["n_members"]
    pooled_grid = [g for g in summary["grid"] if g.get("n_members")]
    assert pooled_grid
    for g in pooled_grid:
        assert g["members"] and len(g["members"]) == g["n_members"]


def test_campaign_summary_raises_on_broken_member_chain(tmp_path):
    # A pooled value reported with n_members>0 but an empty members cell (the column is present) →
    # the evidence chain is broken and the summary refuses to certify it.
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    with pytest.raises(RuntimeError, match="evidence chain broken"):
        _run(tmp_path, _objective(), client, make_mock_runner(omega, blank_members=True))


def test_campaign_end_renders_report_once(tmp_path, monkeypatch):
    # Stage 3: after the loop (and any certify) the campaign re-renders the report ONCE via an
    # `isar report` subprocess, wrapped in a `report_render` timing span. The end-render uses
    # subprocess directly (not the runner seam) — monkeypatch it so the test stays fast and doesn't
    # depend on a real render succeeding; assert the timing span was recorded regardless.
    calls = []

    class _CP:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(loop_mod.subprocess, "run",
                        lambda cmd, **k: (calls.append(list(cmd)), _CP())[1])

    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client, make_mock_runner(omega))

    events = read_log(summary["session_log"])
    renders = [e for e in events if e["kind"] == "timing"
               and (e.get("payload") or {}).get("stage") == "report_render"]
    assert len(renders) == 1                      # rendered exactly once, at the end
    assert isinstance(renders[0]["payload"]["ok"], bool)
    # ... and it invoked `isar report --workspace <ws>` (not a per-iteration `isar run`).
    report_calls = [c for c in calls if len(c) > 3 and c[3] == "report"]
    assert report_calls and "--workspace" in report_calls[-1]
    # The Part-2 diagnostic render fires right after the report render (its own timing span), and the
    # stop event remains the terminal log event.
    diags = [e for e in events if e["kind"] == "timing"
             and (e.get("payload") or {}).get("stage") == "diagnostic_render"]
    assert len(diags) == 1
    diag_calls = [c for c in calls if len(c) > 3 and c[3] == "diagnose"]
    assert diag_calls and "--workspace" in diag_calls[-1]
    assert events[-1]["kind"] == "stop"


def test_campaign_end_renders_campaign_diagnostic(tmp_path):
    # Part 2 full-stack: after a met campaign (which runs the certify pass → logs an escalate_fit
    # promote) the diagnostic renders to report/CAMPAIGN_DIAGNOSTIC.md and its §3 narrative reflects
    # the Part-1 events (escalate_fit promotion count; decide action model-update count).
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client, make_mock_runner(omega, hesse_only=True))
    assert summary["stop"]["state"] == "met", summary["stop"]["reasons"]
    diag = ws / "report" / "CAMPAIGN_DIAGNOSTIC.md"
    assert diag.exists()
    text = diag.read_text(encoding="utf-8")
    assert "## 3. What the driver did" in text
    assert "full-tier promotions" in text          # escalate_fit present (certify_pass promote)
    assert "Model updates." in text                # decide action counter (Part-1 decide.action)
    # stop remains the terminal log event even with the diagnostic render inserted
    assert read_log(summary["session_log"])[-1]["kind"] == "stop"


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
    # ω sits beyond the survey ceiling (SURVEY_E_HI_MEV=7) so even the wide unresolved-q survey window
    # misses it — the first scan then re-centers. (An ω inside the survey band is now caught outright,
    # which is the survey window's whole point; the recenter is the fallback for a truly off-band peak.)
    omega = lambda h: 9.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client,
                       make_mock_runner(omega, window_gate=True), max_iters=60)
    events = read_log(summary["session_log"])
    recenters = [e for e in events if e["kind"] == "exception"
                 and e["payload"].get("action") == "recenter"]
    assert recenters, "a missed window should have logged a recenter action"


# --------------------------------------------------------------------------- escalate_fit logging
def make_untrusted_hesse_runner(omega, *, window_gate=True):
    """A fake `isar run` whose PRODUCTION fits carry only an UNtrusted HESSE interval (no bootstrap):
    once a q has one such production row, the next production there is promoted to the full error tier
    (`_q_needs_full_errors`). Quicklooks carry only an ``omega0_err`` (no interval), so nothing at the q
    ever looks trustworthy and the promotion keeps firing. Nothing certifies → the q never converges."""
    assigned: dict[str, int] = {}

    def runner(workspace, data_paths, **kw):
        data_dir = Path(data_paths[0])
        rows = []
        prod_per_h: dict[float, int] = {}
        for f in sorted(data_dir.glob("*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            params = d.get("launch", {}).get("parameters", {})
            h, k, l = params.get("H"), params.get("K"), params.get("L")
            n = params.get("number_neutrons")
            if f.stem not in assigned:
                assigned[f.stem] = len(assigned) + 1
            scan_id = assigned[f.stem]
            grid = _parse_grid(params["scan_command1"])
            true = omega(h)
            contained = (grid[0] <= true <= grid[-1]) if window_gate else True
            base = {"scan": scan_id, "detector": "det", "h": h, "k": k, "l": l, "status": "fitted"}
            if not contained:
                base.update({"verdict": "no_signal", "disposition": "declined", "omega0": None})
            elif n <= QUICKLOOK_NCOUNT:
                base.update({"verdict": "resolved", "disposition": "resolved", "omega0": true,
                             "omega0_err": 0.6, "confidence": 0.9})
            else:
                base.update({"verdict": "resolved", "disposition": "resolved", "omega0": true,
                             "hesse_lo": true - 0.3, "hesse_hi": true + 0.3, "hesse_trusted": False,
                             "interval_reliable": False, "confidence": 0.9})
                prod_per_h[h] = prod_per_h.get(h, 0) + 1
            rows.append(base)
        master_scan = 9_000_000
        for h, npool in sorted(prod_per_h.items()):
            true = omega(h)
            rows.append({"scan": master_scan, "detector": "det", "h": h, "k": 0.0, "l": 0.0,
                         "status": "fitted", "verdict": "resolved", "disposition": "resolved",
                         "omega0": true, "hesse_lo": true - 0.3, "hesse_hi": true + 0.3,
                         "hesse_trusted": False, "interval_reliable": False, "pooled": True,
                         "n_members": npool, "members": ";".join(str(i) for i in range(npool)),
                         "confidence": 0.9})
            master_scan += 1
        report = Path(workspace) / "report"
        report.mkdir(parents=True, exist_ok=True)
        cols = ["scan", "detector", "h", "k", "l", "status", "verdict", "disposition", "confidence",
                "omega0", "omega0_err", "interval_lo", "interval_hi", "interval_reliable",
                "hesse_lo", "hesse_hi", "hesse_trusted", "pooled", "n_members", "members", "reasons"]
        with open(report / "results.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c) for c in cols})
    return runner


def test_full_tier_promotion_logs_paired_escalate_fit_events(tmp_path):
    # A q whose productions only ever get an untrusted HESSE interval promotes to the full tier on the
    # repeat production. Each promotion must appear as a paired escalate_fit promote/outcome record.
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client, make_untrusted_hesse_runner(omega),
                       max_iters=80)
    events = read_log(summary["session_log"])
    esc = [e for e in events if e["kind"] == "escalate_fit"]
    promotes = [e for e in esc if (e["payload"] or {}).get("phase") == "promote"]
    outcomes = [e for e in esc if (e["payload"] or {}).get("phase") == "outcome"]
    assert promotes, "a repeat untrusted-HESSE production should promote to the full tier"
    assert outcomes, "every promotion should log its outcome"
    # No escalate_fit event was logged as an unknown kind (the vocabulary now includes it).
    assert not any(e.get("kind_unknown") for e in esc)
    # Every in-loop promote (iter not None) is to the full tier with the documented reason.
    inloop = [p for p in promotes if p["payload"].get("iter") is not None]
    assert inloop
    for p in inloop:
        assert p["payload"]["to_tier"] == "full"
        assert p["payload"]["reason"] == "untrusted_hesse_no_reliable_interval"
    # Each in-loop promotion is paired with exactly one outcome at the same iteration, carrying a valid
    # enum and a job_id that traces back to a real run event.
    run_job_ids = {(e["payload"] or {}).get("job_id") for e in events if e["kind"] == "run"}
    assert len(outcomes) == len(inloop)
    for o in outcomes:
        assert o["payload"]["outcome"] in ("seated", "failed", "superseded")
        assert o["payload"]["job_id"] in run_job_ids
        assert any(p["payload"].get("iter") == o["payload"]["iter"] for p in inloop)
    # Every decide event carries the additive `action` field mirroring its `event`.
    decides = [e["payload"] for e in events if e["kind"] == "decide"]
    assert decides
    for d in decides:
        assert d.get("action") == d.get("event")


# --------------------------------------------------------------------------- crash-safe resume (Part C)
def test_resume_state_recovers_ledger_from_log_and_results(tmp_path):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    log = ws / "report" / "session_log.jsonl"
    # Two submitted scans at the same q (a quick-look then a production) — the durable spend ledger.
    log_event(log, "run", {"job_id": "j1", "state": "done", "eta": {"estimated_seconds": 5.0},
                           "kind": "quicklook", "ncount": QUICKLOOK_NCOUNT, "n_points": 50,
                           "hkl": [2.1, 0.0, 0.0], "cost": 5.0e8})
    log_event(log, "run", {"job_id": "j2", "state": "done", "eta": {"estimated_seconds": 7.0},
                           "kind": "production", "ncount": PRODUCTION_NCOUNT, "n_points": 60,
                           "hkl": [2.1, 0.0, 0.0], "cost": 6.0e9})
    _write_results(ws, [{"scan": 2, "detector": "det", "h": 2.1, "k": 0.0, "l": 0.0,
                         "status": "fitted", "verdict": "resolved", "disposition": "resolved",
                         "omega0": 0.2, "interval_lo": 0.05, "interval_hi": 0.35,
                         "interval_reliable": True}])
    r = _resume_state(ws, log, _UNITY_CAL)

    assert r["scan_counter"] == 2                       # exact — no seed reuse / idempotency collision
    assert r["neutrons"] == pytest.approx(5.0e8 + 6.0e9)  # exact spend — no double-spend on restart
    assert r["seconds"] == pytest.approx(12.0)
    assert ("2", "det") in r["consumed"] or (2, "det") in r["consumed"]  # old row never re-folded
    obs = r["observations"]
    assert len(obs) == 1 and obs[0]["kind"] == "production"
    assert sum(o["proposal_cost"] for o in obs) == pytest.approx(5.0e8 + 6.0e9)  # per-q spend recovered


def test_resume_state_attributes_per_row_kinds_not_hkl_lock(tmp_path):
    # One q submitted as a quick-look then two productions (3 result rows). After resume each
    # reconstructed observation must carry its OWN submission's kind (drawn from the per-q ledger in
    # order) — the presence of production rows must NOT relabel the quicklook row as production and
    # inflate n_production (the bug that ate MAX_PROD_ATTEMPTS early → premature 'stuck').
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    log = ws / "report" / "session_log.jsonl"
    for jid, kind, nc, cost in [("j1", "quicklook", QUICKLOOK_NCOUNT, 5.0e8),
                                ("j2", "production", PRODUCTION_NCOUNT, 6.0e9),
                                ("j3", "production", PRODUCTION_NCOUNT, 7.0e9)]:
        log_event(log, "run", {"job_id": jid, "state": "done", "eta": {"estimated_seconds": 1.0},
                               "kind": kind, "ncount": nc, "n_points": 50,
                               "hkl": [2.1, 0.0, 0.0], "cost": cost})
    base = {"h": 2.1, "k": 0.0, "l": 0.0, "status": "fitted", "verdict": "resolved",
            "disposition": "resolved", "omega0": 0.2, "interval_lo": 0.05, "interval_hi": 0.35,
            "interval_reliable": True}
    _write_results(ws, [{"scan": s, "detector": "det", **base} for s in (1, 2, 3)])
    r = _resume_state(ws, log, _UNITY_CAL)

    obs = r["observations"]
    kinds = sorted(o["kind"] for o in obs)
    assert kinds == ["production", "production", "quicklook"]     # per-row, not hkl-locked
    assert sum(o["kind"] == "production" for o in obs) == 2       # n_production not inflated
    # The whole q spend lumped onto exactly one row, and that row is a PRODUCTION row.
    cost_rows = [o for o in obs if o["proposal_cost"] > 0]
    assert len(cost_rows) == 1 and cost_rows[0]["kind"] == "production"
    assert sum(o["proposal_cost"] for o in obs) == pytest.approx(5.0e8 + 6.0e9 + 7.0e9)


def test_cold_workspace_resumes_to_fresh(tmp_path):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    r = _resume_state(ws, ws / "report" / "session_log.jsonl", _UNITY_CAL)
    assert r == {"observations": [], "consumed": set(), "scan_kind": {},
                 "scan_counter": 0, "neutrons": 0.0, "seconds": 0.0}


# --------------------------------------------------------------------------- profiling / timing (step 4)
def test_campaign_emits_timing_events_covering_core_stages(tmp_path):
    # A full fake-seam campaign appends "timing" events for the instrumented stages, each carrying a
    # stage/seconds/ok payload and (in-loop) an iteration index.
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client, make_mock_runner(omega))
    timings = [e["payload"] for e in read_log(summary["session_log"]) if e["kind"] == "timing"]
    stages = {t["stage"] for t in timings}
    for expected in ("stop_eval", "model_fit", "propose", "validate", "submit",
                     "wait_scan", "isar_run", "iteration"):
        assert expected in stages, f"missing timing stage {expected} in {sorted(stages)}"
    # Every timing event has the documented span shape.
    for t in timings:
        assert set(("stage", "iter", "seconds", "ok")) <= set(t)
        assert isinstance(t["seconds"], (int, float)) and t["ok"] is True
    # In-loop spans carry an integer iteration index (the certify span uses iter=None).
    assert any(isinstance(t["iter"], int) for t in timings if t["stage"] == "iteration")


def _runner_with_timings(omega, timings_doc, **kw):
    """Wrap make_mock_runner so each invocation ALSO drops report/run_timings.json (as `isar run` does),
    letting the loop embed it into the analyze event."""
    inner = make_mock_runner(omega, **kw)

    def runner(workspace, data_paths, **rk):
        inner(workspace, data_paths, **rk)
        report = Path(workspace) / "report"
        report.mkdir(parents=True, exist_ok=True)
        (report / "run_timings.json").write_text(json.dumps(timings_doc), encoding="utf-8")
    return runner


def test_analyze_carries_embedded_run_timings(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    doc = {"total_seconds": 1.23, "argv": ["isar", "run"], "errors_tier": "fast", "M": 200,
           "stages": [{"stage": "ingest_parse", "seconds": 0.4}]}
    summary, ws = _run(tmp_path, _objective(), client, _runner_with_timings(omega, doc))
    analyzes = [e["payload"] for e in read_log(summary["session_log"]) if e["kind"] == "analyze"]
    assert analyzes, "campaign produced no analyze events"
    embedded = [a for a in analyzes if a.get("timings") is not None]
    assert embedded, "no analyze event embedded run_timings.json"
    a = embedded[0]
    assert a["timings"]["total_seconds"] == 1.23
    assert "n_rows" in a and isinstance(a["n_rows"], int)
    assert "dispositions" in a and isinstance(a["dispositions"], list)


def test_resume_state_ignores_timing_events(tmp_path):
    # RESUME REGRESSION: timing events are inert to _resume_state — the recovered spend ledger is
    # byte-for-byte identical to the same log with the timing events stripped out.
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    log = ws / "report" / "session_log.jsonl"
    log_clean = ws / "report" / "session_log_clean.jsonl"
    log_event(log, "run", {"job_id": "j1", "state": "done", "eta": {"estimated_seconds": 5.0},
                           "kind": "quicklook", "ncount": QUICKLOOK_NCOUNT, "n_points": 50,
                           "hkl": [2.1, 0.0, 0.0], "cost": 5.0e8})
    log_event(log, "timing", {"stage": "isar_run", "iter": 0, "seconds": 143.2, "ok": True})
    log_event(log, "run", {"job_id": "j2", "state": "done", "eta": {"estimated_seconds": 7.0},
                           "kind": "production", "ncount": PRODUCTION_NCOUNT, "n_points": 60,
                           "hkl": [2.1, 0.0, 0.0], "cost": 6.0e9})
    log_event(log, "timing", {"stage": "iteration", "iter": 0, "seconds": 150.0, "ok": True})
    # The same ledger without any timing events.
    log_event(log_clean, "run", {"job_id": "j1", "state": "done", "eta": {"estimated_seconds": 5.0},
                                 "kind": "quicklook", "ncount": QUICKLOOK_NCOUNT, "n_points": 50,
                                 "hkl": [2.1, 0.0, 0.0], "cost": 5.0e8})
    log_event(log_clean, "run", {"job_id": "j2", "state": "done", "eta": {"estimated_seconds": 7.0},
                                 "kind": "production", "ncount": PRODUCTION_NCOUNT, "n_points": 60,
                                 "hkl": [2.1, 0.0, 0.0], "cost": 6.0e9})
    _write_results(ws, [{"scan": 2, "detector": "det", "h": 2.1, "k": 0.0, "l": 0.0,
                         "status": "fitted", "verdict": "resolved", "disposition": "resolved",
                         "omega0": 0.2, "interval_lo": 0.05, "interval_hi": 0.35,
                         "interval_reliable": True}])
    r_timed = _resume_state(ws, log, _UNITY_CAL)
    r_clean = _resume_state(ws, log_clean, _UNITY_CAL)
    assert r_timed["scan_counter"] == r_clean["scan_counter"]
    assert r_timed["neutrons"] == pytest.approx(r_clean["neutrons"])
    assert r_timed["seconds"] == pytest.approx(r_clean["seconds"])
    assert r_timed["scan_counter"] == 2


def test_restart_recovers_budget_and_does_not_respend(tmp_path):
    # Run a full campaign, then RESTART run_campaign in the same workspace: it recovers the spend
    # ledger from the durable files (no reset to zero), recognizes convergence from the pooled masters,
    # and does not re-charge budget for the already-measured scans.
    omega = lambda h: (h - 2.0) * 2.0
    summary1, ws = _run(tmp_path, _objective(), MockTavi(omega), make_mock_runner(omega))
    assert summary1["stop"]["state"] == "met"
    spent1 = summary1["budget"]["neutrons"]
    assert spent1 > 0

    cfg = {"workspace": ws, "client": MockTavi(omega), "run_isar": make_mock_runner(omega),
           "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8}, "seed": 7}
    summary2 = run_campaign(_objective(), cfg)
    start = [e for e in read_log(summary2["session_log"])
             if (e.get("payload") or {}).get("event") == "campaign_start"][-1]
    assert start["payload"]["resumed"]["scan_counter"] > 0
    assert start["payload"]["resumed"]["spent"] == pytest.approx(spent1)
    assert summary2["stop"]["state"] == "met"           # already converged from the persisted masters


# --------------------------------------------------------------------------- idempotency run nonce
def test_run_nonce_mints_and_persists(tmp_path):
    ws = tmp_path / "ws"
    nonce1 = _run_nonce(ws)
    assert (ws / ".run_nonce").exists()
    assert (ws / ".run_nonce").read_text(encoding="utf-8").strip() == nonce1
    # a second call against the SAME workspace returns the identical value
    nonce2 = _run_nonce(ws)
    assert nonce2 == nonce1
    # a call against a fresh (different) workspace mints a different nonce
    ws2 = tmp_path / "ws2"
    nonce3 = _run_nonce(ws2)
    assert nonce3 != nonce1


def test_campaign_config_sets_run_nonce_from_workspace_file(tmp_path):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    cfg = _campaign_config(_objective(), {"workspace": ws, "client": MockTavi(lambda h: 0.0),
                                          "run_isar": lambda *a, **k: None,
                                          "calibration": _UNITY_CAL})
    assert cfg.run_nonce == (ws / ".run_nonce").read_text(encoding="utf-8").strip()
    assert cfg.run_nonce != ""
    # resolving the config again against the same workspace reuses the persisted nonce
    cfg2 = _campaign_config(_objective(), {"workspace": ws, "client": MockTavi(lambda h: 0.0),
                                           "run_isar": lambda *a, **k: None,
                                           "calibration": _UNITY_CAL})
    assert cfg2.run_nonce == cfg.run_nonce


def test_campaign_submit_idempotency_key_contains_run_nonce(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    calls = []
    orig_submit = client.submit_scan

    def spy_submit(body, idempotency_key=None):
        calls.append(idempotency_key)
        return orig_submit(body, idempotency_key=idempotency_key)
    client.submit_scan = spy_submit

    summary, ws = _run(tmp_path, _objective(), client, make_mock_runner(omega))
    nonce = (ws / ".run_nonce").read_text(encoding="utf-8").strip()
    assert calls
    assert all(nonce in key for key in calls)
