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
from isar.drive.loop import (DEFAULT_MAX_POINTS, _campaign_config, _emitted_point_count,
                             _campaign_summary,
                             _terminalize_certification_regression,
                             _declined_observation,
                             _executed_point_count, _feasible_point_count,
                             _errors_arg_for, _errors_tier_for, _errors_tiers_for,
                             _extract_max_points, _full_tier,
                             _finalize_proposal, _guard_point_count,
                             _make_classification_providers, _record_eligible_crossings,
                             _pending_certification_indices, _record_certification_acceptance,
                             _master_obs_from_row, _observation_from_row, _pick_interval,
                             _pooled_target_delivered,
                             _preflight_instrument_limits, _preflight_max_points,
                             _reconcile_budget_points, _resume_state,
                             _run_nonce, _target_supporting_feasible_points, _validate_body,
                             run_campaign)
from isar.drive.acquisition.acquire import ScanProposal, points_in_window
from isar.drive.acquisition.eig import counts_eig
from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot
from isar.drive.instrument.tavi_client import TaviError
from isar.drive.isar_io.scan_writer import save_scan_json
from isar.drive.session_log import log_event, read_log
from isar.drive.supervisor.stopping import (
    GridQ,
    PRODUCTION_NCOUNT,
    QUICKLOOK_NCOUNT,
    StopDecision,
    summarize_grid,
)
from isar.schema.hypotheses import content_fingerprint

_UNITY_CAL = Calibration.from_dict({"inflation": 1.0, "buckets": [], "center_pull_mean": 0.0})


def test_certification_regression_is_terminal_and_resumable():
    per_q = [
        {"hkl": [2.7, 0.0, 0.0], "converged": False,
         "calibrated_halfwidth": 0.111381, "tolerance": 0.1},
        {"hkl": [2.8, 0.0, 0.0], "converged": True,
         "calibrated_halfwidth": 0.08, "tolerance": 0.1},
    ]
    result = _terminalize_certification_regression(StopDecision("continue", [], per_q))

    assert result.state == "incomplete"
    assert result.per_q == per_q
    assert "resume the campaign" in result.reasons[0]
    assert "(2.7, 0, 0): ±0.1114 > ±0.1 meV" in result.reasons[0]


def test_certification_regression_terminalizer_preserves_other_outcomes():
    decision = StopDecision("infeasible", ["instrument limit"], [])
    assert _terminalize_certification_regression(decision) is decision


def test_feasible_point_count_prefers_tavi_contract_fields():
    assert _feasible_point_count({"feasible_points": 2,
                                  "planned_feasible_mask": [True, True, True]}, 9) == 2
    assert _feasible_point_count({"planned_feasible_mask": [True, False, True]}, 9) == 2
    assert _feasible_point_count({}, 9) == 9


def test_executed_point_count_uses_frozen_execution_mask():
    data = {"result": {"executed_feasible_mask": [True, False, True]}}
    assert _executed_point_count(data, 9) == 2
    assert _executed_point_count({"result": {}}, 9) == 9


def test_target_support_preflight_counts_only_feasible_bins_inside_credible_band():
    validation = {"point_manifest": [
        {"feasible": True, "values": {"deltaE": 5.8}},
        {"feasible": False, "values": {"deltaE": 6.0}},
        {"feasible": True, "values": {"deltaE": 6.2}},
        {"feasible": True, "values": {"deltaE": 10.0}},  # feasible background is not support
    ]}
    assert _target_supporting_feasible_points(validation, (6.0, 0.2)) == 2
    assert _target_supporting_feasible_points({}, (6.0, 0.2)) is None
    malformed = {"point_manifest": [{"feasible": True, "values": {"deltaE": "bad"}}]}
    assert _target_supporting_feasible_points(malformed, (6.0, 0.2)) == 0


def test_classification_uses_latest_complete_temperature_for_active_sample_only():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    objective = ObjectiveSpec.segment(
        q_start=(2.0, 0.0, 0.0), q_end=(2.0, 0.1, 0.0), step_rlu=0.1,
        tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1e12},
        target_branch="optic", energy_search_meV=(-2.0, 12.0), **frozen)

    def row(sample, temperature, branch, energy):
        return {"sample": sample, "t_bin": temperature, "branch_id": branch,
                "anchors": [{"u": u, "omega0": energy, "weight": 1.0}
                            for u in (0.0, 0.1, 0.2)],
                "n_anchors": 3, "period": None,
                "parameter_prior": next(h for h in objective.hypotheses
                                        if h["name"] == branch)}

    selected_acoustic = row("active", 200.0, "acoustic", 2.0)
    state = {"branch_belief_models": [
        row("active", 100.0, "acoustic", 1.0), row("active", 100.0, "optic", 6.0),
        selected_acoustic, row("active", 200.0, "optic", 7.0),
        row("active", 300.0, "optic", 8.0),  # newest but incomplete: never mix with 200 K
        row("other", 400.0, "acoustic", 9.0), row("other", 400.0, "optic", 10.0),
    ]}
    classify, rival = _make_classification_providers(
        state, objective, 1.0, sample="active")
    gridq = types.SimpleNamespace(hkl=(2.0, 0.0, 0.0), q=0.0)
    assert classify(gridq) >= 0.0
    from isar.drive.acquisition.classify_eig import belief_predict
    assert rival(gridq) == pytest.approx(belief_predict(selected_acoustic, 0.0))
    fallback, fallback_rival = _make_classification_providers(
        state, objective, 1.0, sample="missing")
    assert fallback is not None and fallback_rival is not None

    state["branch_tracks"] = {"assignments": [{
        "q": [2.0, 0.0, 0.0], "acoustic_candidate_index": 0,
        "candidates": [{"probabilities": {"acoustic": 0.999, "optic": 0.0005,
                                            "new": 0.0005}}],
    }]}
    resolved_classify, _ = _make_classification_providers(
        state, objective, 1.0, sample="active")
    assert resolved_classify(gridq) == 0.0

    acoustic_objective = ObjectiveSpec.segment(
        q_start=(2.0, 0.0, 0.0), q_end=(2.0, 0.1, 0.0), step_rlu=0.1,
        tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1e12},
        target_branch="acoustic", energy_search_meV=(-2.0, 12.0),
        **shipped_hypothesis_snapshot("acoustic", (-2.0, 12.0)))
    state["branch_tracks"] = {"assignments": [{
        "q": [2.0, 0.0, 0.0], "optic_candidate_index": 0,
        "candidates": [{"probabilities": {"acoustic": 0.0005, "optic": 0.999,
                                            "new": 0.0005}}],
    }]}
    acoustic_classify, _ = _make_classification_providers(
        state, acoustic_objective, 1.0, sample="active")
    assert acoustic_classify(gridq) == 0.0


def test_scan_declines_do_not_make_a_tracked_frontier_terminal():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    objective = ObjectiveSpec(
        path=[(2.0, 0.0, 0.0), (2.05, 0.0, 0.0)], tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e12}, target_branch="optic",
        target_selector="tracked_branch", **frozen)
    anchor = GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5)
    anchor.track_state = "ambiguous"
    anchor.n_decline = 3
    neighbor = GridQ(index=1, hkl=(2.05, 0.0, 0.0), q=0.05, tolerance=0.5)
    decision = loop_mod._blocked_frontier_decision(
        objective, [anchor, neighbor], StopDecision("continue", [], []))
    assert decision.state == "continue"
    assert decision.reasons == []


def _blocked_identity_objective(branch="acoustic"):
    frozen = shipped_hypothesis_snapshot(branch, (-2.0, 12.0))
    return ObjectiveSpec(
        path=[(2.0, 0.0, 0.0), (2.05, 0.0, 0.0)], tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e12}, target_branch=branch,
        target_selector="tracked_branch", **frozen)


def test_exhausted_tracked_identity_route_is_honestly_incomplete():
    """Honest incomplete requires the same-q ceiling actually reached, not merely a blocked route.

    Before the OP10 repair this fixture used n_production=1 against a ceiling of eight and still
    reported a scientific incomplete.  That is the exact shape of the OP10 failure, so it is now an
    invariant violation (covered below) rather than a result.
    """
    objective = _blocked_identity_objective()
    anchor = GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5,
                   n_quicklook=1, track_state="ambiguous", max_production_batches=8)
    anchor.n_production = 8
    anchor.evidence_master = {
        "branch_candidates": [{"energy_meV": 5.98, "sigma_meV": 0.05}],
        "diagnostic_halfwidth": 0.05,
    }
    neighbor = GridQ(index=1, hkl=(2.05, 0.0, 0.0), q=0.05, tolerance=0.5,
                     n_quicklook=1, track_state="blocked")
    decision = loop_mod._blocked_frontier_decision(
        objective, [anchor, neighbor], StopDecision("continue", [], []))
    assert decision.state == "incomplete"
    assert "branch_ambiguity" in decision.reasons[0]


def test_proposal_less_frontier_with_same_q_budget_left_is_a_controller_fault():
    """OP10 regression: a stoppable-looking frontier that still has authorized scans is a bug.

    OP10 reported ``branch_ambiguity`` at six points whose n_production was 0 or 1 against an
    authorized 12, with exposure_saturated False.  Presenting that as a scientific result hid a
    controller defect, so the loop must fail loudly instead.
    """
    objective = _blocked_identity_objective()
    anchor = GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5,
                   n_quicklook=1, track_state="ambiguous", max_production_batches=12)
    anchor.n_production = 1
    anchor.local_identity_clear = False
    anchor.evidence_master = {
        "branch_candidates": [{"energy_meV": 5.98, "sigma_meV": 0.05}],
        "diagnostic_halfwidth": 0.05,
    }
    neighbor = GridQ(index=1, hkl=(2.05, 0.0, 0.0), q=0.05, tolerance=0.5,
                     n_quicklook=1, track_state="blocked")
    with pytest.raises(loop_mod.ControllerInvariantError) as excinfo:
        loop_mod._blocked_frontier_decision(
            objective, [anchor, neighbor], StopDecision("continue", [], []))
    message = str(excinfo.value)
    assert "1/12" in message                       # the unspent budget is named
    assert "path index 0" in message


def test_pooled_selected_optic_suppresses_false_missed_window_from_acoustic_individual():
    proposal = ScanProposal(
        h=2.05, k=0.0, l=0.0, e_lo=-2.0, e_hi=8.0, e_step=0.25,
        ncount=25_000_000, kind="production", reason="frontier revisit",
        e_pred=6.0, acquisition_id="acq-1")
    observations = [{
        "kind": "pooled_master_update", "acquisition_id": "acq-1",
        "hkl": [2.05, 0.0, 0.0], "usable": True, "branch_id": "optic",
        "selected_candidate_index": 1, "omega0": 6.2,
        "branch_candidates": [{"energy_meV": 0.46}, {"energy_meV": 6.2}],
    }]
    assert _pooled_target_delivered(observations, proposal, "optic")
    assert not _pooled_target_delivered(observations, proposal, "acoustic")


def test_crossing_recorder_records_transitions_not_every_eligible_batch(tmp_path):
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    objective = ObjectiveSpec(path=[(2.0, 0.0, 0.0)], tolerance_meV=0.5,
                              budget={"kind": "neutrons", "amount": 1e12},
                              target_branch="optic", **frozen)

    def update(halfwidth):
        return {"kind": "pooled_master_update", "hkl": (2.0, 0.0, 0.0),
                "branch_id": "optic", "halfwidth": halfwidth, "usable": True,
                "certifiable": True}

    def master(members, halfwidth=0.3):
        return {"hkl": (2.0, 0.0, 0.0), "branch_id": "optic", "halfwidth": halfwidth,
                "usable": True, "reliable": True, "certifiable": True, "omega0": 6.0,
                "members": members}

    observations = [update(0.3)]
    grid = summarize_grid(objective, observations, [master(["A"])])
    assert _record_eligible_crossings(grid, observations, tmp_path / "crossings.jsonl") == 1
    observations.append(update(0.25))
    grid = summarize_grid(objective, observations, [master(["A", "B"], 0.25)])
    assert _record_eligible_crossings(grid, observations) == 0
    assert grid[0].converged
    observations.extend([update(0.8), update(0.3)])
    grid = summarize_grid(objective, observations, [master(["A"], 0.3)])
    assert _record_eligible_crossings(grid, observations) == 1


def test_grid_summary_replays_authoritative_revision_and_identity_revisit_chronology():
    frozen = shipped_hypothesis_snapshot("acoustic", (-2.0, 12.0))
    objective = ObjectiveSpec(
        path=[(2.05, 0.0, 0.0), (2.10, 0.0, 0.0)], tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e12}, target_branch="acoustic",
        target_selector="tracked_branch", **frozen)
    candidates = [{"energy_meV": 0.9, "sigma_meV": 0.08}]
    observations = [
        {"hkl": [2.10, 0.0, 0.0], "kind": "production", "acquisition_id": "initial",
         "acquisition_action_role": "production", "resolved": False},
        {"hkl": [2.10, 0.0, 0.0], "kind": "pooled_master_update",
         "branch_candidates": candidates, "content_hash": "middle-r1"},
        {"hkl": [2.05, 0.0, 0.0], "kind": "quicklook", "acquisition_id": "neighbor",
         "acquisition_action_role": "classification", "resolved": False},
        {"hkl": [2.05, 0.0, 0.0], "kind": "pooled_master_update",
         "branch_candidates": candidates, "content_hash": "neighbor-r1"},
        {"hkl": [2.10, 0.0, 0.0], "kind": "production", "acquisition_id": "revisit",
         "acquisition_action_role": "classification", "resolved": False},
        {"hkl": [2.10, 0.0, 0.0], "kind": "pooled_master_update",
         "branch_candidates": candidates, "content_hash": "middle-r2"},
    ]
    grid = summarize_grid(objective, observations, [])
    middle = grid[1]
    assert middle.production_sequences == [0, 4]
    assert middle.identity_revisit_sequences == [4]
    assert middle.evidence_update_sequences == [1, 5]
    assert grid[0].evidence_update_sequences == [3]


def test_budget_reconciliation_charges_only_executed_points():
    budget = {"kind": "neutrons", "neutrons": 300.0, "seconds": 0.0, "spent": 300.0}
    _reconcile_budget_points(budget, ncount=100, planned=3, executed=2)
    assert budget == {"kind": "neutrons", "neutrons": 200.0,
                      "seconds": 0.0, "spent": 200.0}


def test_budget_reconciliation_scales_time_and_returns_durable_deltas():
    budget = {"kind": "counting_seconds", "neutrons": 300.0,
              "seconds": 30.0, "spent": 30.0}
    deltas = _reconcile_budget_points(budget, ncount=100, planned=3, executed=2, eta=30.0)
    assert deltas == pytest.approx((-100.0, -10.0))
    assert budget["neutrons"] == pytest.approx(200.0)
    assert budget["seconds"] == pytest.approx(20.0)
    assert budget["spent"] == pytest.approx(20.0)


def test_campaign_summary_persists_hypothesis_mismatch_not_just_a_log(monkeypatch, tmp_path):
    mismatch = {"active": True, "comparison": "prior_vs_data_only_posterior_predictive"}
    monkeypatch.setattr(loop_mod, "read_card_state", lambda _workspace: {
        "dispersion_prior": [], "branch_belief": [{"branch_id": "optic"}],
        "hypothesis_mismatch": mismatch})
    decision = types.SimpleNamespace(to_dict=lambda: {"state": "continue", "reasons": [], "per_q": []})
    summary = _campaign_summary(_objective(), [], decision,
                                {"kind": "neutrons", "spent": 0}, tmp_path,
                                tmp_path / "session.jsonl", 0)
    assert summary["hypothesis_mismatch"] == mismatch
    assert summary["branch_beliefs"][0]["branch_id"] == "optic"


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


class _SubmitFailingTavi(MockTavi):
    def submit_scan(self, body, idempotency_key=None):
        raise TaviError(503, "POST /scan -> HTTP 503: instrument unavailable")


class _WaitFailingTavi(MockTavi):
    def wait_scan(self, job_id, timeout=0.0, **kw):
        raise TaviError(None, f"GET /scan/{job_id} -> transport error: connection lost")


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
                    "model_interval_lo": true - hw, "model_interval_med": true,
                    "model_interval_hi": true + hw, "model_robust": True,
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
                "pooled", "n_members", "members", "model_interval_lo", "model_interval_med",
                "model_interval_hi", "model_robust", "reasons"]
        with open(report / "results.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c) for c in cols})
    return runner


class BranchAwareOpticTavi(MockTavi):
    """Deterministic two-branch spectrum: acoustic rises from zero; optic stays near 6 meV."""

    @staticmethod
    def optic(k):
        return 6.0 + 0.4 * float(k)

    @staticmethod
    def acoustic(k):
        return 12.0 * abs(float(k))

    def get_scan_data(self, job_id):
        body = self.jobs[job_id]
        params = body["parameters"]
        grid = _parse_grid(params["scan_command1"])
        optic = self.optic(params["K"])
        acoustic = self.acoustic(params["K"])
        counts = [1.0 + 75.0 * _math.exp(-((energy - optic) / 0.35) ** 2)
                  + 120.0 * _math.exp(-((energy - acoustic) / 0.30) ** 2)
                  for energy in grid]
        return {"job_id": job_id, "state": "done",
                "result": {"counts": counts, "scan_values_1": grid},
                "launch": {"parameters": params}}


def make_branch_aware_optic_runner(objective):
    """Analysis seam for the orchestration regression, with durable two-branch beliefs."""
    assigned: dict[str, int] = {}
    priors = {row["name"]: row for row in objective.hypotheses}
    quicklook_floor = int(objective.steering.get("quicklook_ncount", QUICKLOOK_NCOUNT))
    if not {"optic", "acoustic"} <= set(priors):
        raise AssertionError("two-branch optic fixture requires optic and acoustic hypotheses")

    def runner(workspace, data_paths, **_kwargs):
        data_dir = Path(data_paths[0])
        rows = []
        production: dict[tuple[str, float, float, float], list[int]] = {}
        anchors = {"optic": {}, "acoustic": {}}
        for path in sorted(data_dir.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            params = document.get("launch", {}).get("parameters", {})
            h, k, l = map(float, (params["H"], params["K"], params["L"]))
            ncount = int(params["number_neutrons"])
            if path.stem not in assigned:
                assigned[path.stem] = len(assigned) + 1
            scan_id = assigned[path.stem]
            energy_grid = _parse_grid(params["scan_command1"])
            optic = BranchAwareOpticTavi.optic(k)
            acoustic = BranchAwareOpticTavi.acoustic(k)
            for branch_id, energy in (("optic", optic), ("acoustic", acoustic)):
                if energy_grid[0] <= energy <= energy_grid[-1]:
                    anchors[branch_id][round(k, 8)] = {
                        "u": k, "omega0": energy, "weight": 0.05,
                    }
            optic_contained = energy_grid[0] <= optic <= energy_grid[-1]
            optic_row = {"scan": scan_id, "detector": "det-optic", "h": h, "k": k, "l": l,
                         "status": "fitted", "interval_reliable": True, "branch_id": "optic"}
            if not optic_contained:
                optic_row.update({"verdict": "no_signal", "disposition": "declined", "omega0": None,
                                  "interval_lo": None, "interval_hi": None,
                                  "reasons": "optic peak outside window"})
            else:
                halfwidth = 0.6 if ncount <= quicklook_floor else 0.3
                optic_row.update({"verdict": "resolved", "disposition": "resolved", "omega0": optic,
                                  "interval_lo": optic - halfwidth, "interval_hi": optic + halfwidth,
                                  "confidence": 0.95, "reasons": "optic branch resolved"})
                if ncount > quicklook_floor:
                    production.setdefault(("optic", h, k, l), []).append(scan_id)
            rows.append(optic_row)

            # A second resolved candidate models the stronger acoustic peak in the same spectrum.  It
            # is intentionally present in results.csv so branch filtering—not fixture omission—keeps
            # it out of optic certification and the final dispersion.
            if energy_grid[0] <= acoustic <= energy_grid[-1]:
                halfwidth = 0.5 if ncount <= quicklook_floor else 0.25
                rows.append({
                    "scan": scan_id, "detector": "det-acoustic", "h": h, "k": k, "l": l,
                    "status": "fitted", "interval_reliable": True, "branch_id": "acoustic",
                    "verdict": "resolved", "disposition": "resolved", "omega0": acoustic,
                    "interval_lo": acoustic - halfwidth, "interval_hi": acoustic + halfwidth,
                    "confidence": 0.97, "reasons": "strong acoustic branch resolved",
                })
                if ncount > quicklook_floor:
                    production.setdefault(("acoustic", h, k, l), []).append(scan_id)

        master_scan = 9_100_000
        for (branch_id, h, k, l), members in sorted(production.items()):
            energy = (BranchAwareOpticTavi.optic(k) if branch_id == "optic"
                      else BranchAwareOpticTavi.acoustic(k))
            halfwidth = (0.3 if branch_id == "optic" else 0.25) / _math.sqrt(len(members))
            rows.append({
                "scan": master_scan, "detector": f"det-{branch_id}", "h": h, "k": k, "l": l,
                "status": "fitted", "verdict": "resolved", "disposition": "resolved",
                "omega0": energy, "interval_lo": energy - halfwidth,
                "interval_hi": energy + halfwidth, "interval_reliable": True,
                "model_interval_lo": energy - halfwidth, "model_interval_med": energy,
                "model_interval_hi": energy + halfwidth, "model_robust": True,
                "pooled": True, "n_members": len(members),
                "members": ";".join(map(str, members)), "branch_id": branch_id,
                "confidence": 0.95, "reasons": f"pooled {branch_id} master",
            })
            master_scan += 1

        report = Path(workspace) / "report"
        report.mkdir(parents=True, exist_ok=True)
        columns = ["scan", "detector", "h", "k", "l", "status", "verdict", "disposition",
                   "confidence", "omega0", "interval_lo", "interval_hi", "interval_reliable",
                   "pooled", "n_members", "members", "branch_id", "model_interval_lo",
                   "model_interval_med", "model_interval_hi", "model_robust", "reasons"]
        with open(report / "results.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column) for column in columns})

        belief_models = []
        flat_beliefs = []
        for branch_id in ("optic", "acoustic"):
            branch_anchors = [anchors[branch_id][key] for key in sorted(anchors[branch_id])]
            belief_models.append({
                "sample": "Al_phonon_DFT", "t_bin": 200.0, "branch_id": branch_id,
                "anchors": branch_anchors, "n_anchors": len(branch_anchors), "period": None,
                "parameter_prior": priors[branch_id],
            })
            flat_beliefs.append({
                "sample": "Al_phonon_DFT", "t_bin": 200.0, "branch_id": branch_id,
                "label_probabilities": {branch_id: 0.95},
                "p_is_target": 0.95 if branch_id == "optic" else 0.05,
                "n_anchors": len(branch_anchors),
            })
        (Path(workspace) / "card.state.json").write_text(json.dumps({
            "schema_version": 1, "branch_belief_models": belief_models,
            "branch_belief": flat_beliefs, "dispersion_prior": [],
        }, indent=2), encoding="utf-8")

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


def test_invalid_effective_exposure_is_not_replaced_by_submitted_ncount():
    o = _observation_from_row(
        _urow(interval_lo=4.9, interval_hi=5.1, interval_reliable=True,
              effective_exposure=0.0),
        "production", PRODUCTION_NCOUNT, _UNITY_CAL)
    assert o["effective_exposure"] == 0.0
    assert o["exposure_source"] == "invalid_effective_exposure"
    assert o["exposure_bucket"] == "global"


def test_candidate_rejection_requires_two_acquisitions_and_neighboring_q():
    base = {"kind": "pooled_master_update", "candidate_id": "optic:lowest_energy:unspecified",
            "candidate_state": "provisional", "target_branch": "optic",
            "branch_posterior": {"optic": 0.04, "acoustic": 0.8, "new": 0.16}}
    first = {**base, "hkl": [2.1, 0.0, 0.0], "acquisition_id": "A1"}
    state, evidence = loop_mod._candidate_evidence_state([], first)
    assert state == "provisional" and evidence["n_logical_acquisitions"] == 1

    same_q = {**base, "hkl": [2.1, 0.0, 0.0], "acquisition_id": "A2"}
    state, _ = loop_mod._candidate_evidence_state([first], same_q)
    assert state == "provisional"
    neighbor = {**base, "hkl": [2.2, 0.0, 0.0], "acquisition_id": "A2"}
    state, evidence = loop_mod._candidate_evidence_state([first], neighbor)
    assert state == "rejected"
    assert evidence["n_logical_acquisitions"] == evidence["n_q_positions"] == 2

    supported = {**neighbor, "branch_posterior": {"optic": 0.06, "acoustic": 0.8, "new": 0.14}}
    assert loop_mod._candidate_evidence_state([first], supported)[0] == "provisional"
    boundary = {**neighbor, "branch_posterior": {"optic": 0.05, "acoustic": 0.6, "new": 0.35}}
    assert loop_mod._candidate_evidence_state([first], boundary)[0] == "rejected"
    weak_competitor = {**neighbor,
                       "branch_posterior": {"optic": 0.05, "acoustic": 0.59, "new": 0.36}}
    assert loop_mod._candidate_evidence_state([first], weak_competitor)[0] == "provisional"
    accepted = {**neighbor, "candidate_state": "accepted"}
    assert loop_mod._candidate_evidence_state([first], accepted)[0] == "accepted"
    unknown = {**neighbor, "candidate_state": "unknown"}
    assert loop_mod._candidate_evidence_state([first], unknown)[0] == "unknown"


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


def test_master_gamma_at_bound_certifies_when_model_robust_omega_is_identified():
    m = _master_obs_from_row(
        _urow(disposition="flagged", interval_lo=4.999, interval_hi=5.001, interval_reliable=False,
              omega0_profile_lo=4.99, omega0_profile_hi=5.01,
              model_interval_lo=4.98, model_interval_med=5.0, model_interval_hi=5.02,
              model_robust=True, gamma_at_bound=True),
        _UNITY_CAL)
    assert m["reliable"] is True and m["certifiable"] is True
    assert m["halfwidth"] == pytest.approx(0.02)


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
    # A physical scan is provisional evidence and cannot trigger authoritative escalation.
    _write_results(ws, [{"scan": 1, "detector": "det", "h": 2.1, "k": 0.0, "l": 0.0,
                         "status": "fitted", "disposition": "flagged", "omega0": 0.95,
                         "hesse_lo": 0.94, "hesse_hi": 0.96, "hesse_trusted": False}])
    assert _errors_arg_for(_prop(2.1), ws) == ["--errors", "fast"]
    # The same untrusted result on the pooled master authoritatively promotes only the master tier.
    _write_results(ws, [{"scan": 9000000, "detector": "merged", "h": 2.1, "k": 0.0, "l": 0.0,
                         "status": "fitted", "disposition": "flagged", "omega0": 0.95,
                         "pooled": True,
                         "hesse_lo": 0.94, "hesse_hi": 0.96, "hesse_trusted": False}])
    assert _errors_arg_for(_prop(2.1), ws) == ["--errors", "full"]
    assert _errors_tiers_for(_prop(2.1), ws) == ("fast", "full")
    assert _errors_tiers_for(_prop(2.1, "quicklook"), ws) == ("cheap", "cheap")
    # A trusted-HESSE q stays on the cheap fast tier.
    _write_results(ws, [{"scan": 2, "detector": "det", "h": 2.2, "k": 0.0, "l": 0.0,
                         "status": "fitted", "disposition": "resolved", "omega0": 1.1,
                         "pooled": True, "hesse_lo": 1.09, "hesse_hi": 1.11,
                         "hesse_trusted": True}])
    assert _errors_arg_for(_prop(2.2), ws) == ["--errors", "fast"]


def test_errors_tier_ladder_uses_master_when_physical_and_pooled_rows_disagree(tmp_path):
    ws = tmp_path / "ws"
    _write_results(ws, [
        {"scan": 1, "detector": "det", "h": 2.1, "k": 0.0, "l": 0.0,
         "status": "fitted", "disposition": "resolved", "omega0": 0.95,
         "hesse_lo": 0.94, "hesse_hi": 0.96, "hesse_trusted": True},
        {"scan": 9000000, "detector": "merged", "h": 2.1, "k": 0.0, "l": 0.0,
         "status": "fitted", "disposition": "flagged", "omega0": 0.95,
         "pooled": True, "hesse_lo": 0.94, "hesse_hi": 0.96,
         "hesse_trusted": False},
    ])
    assert _errors_tiers_for(_prop(2.1), ws) == ("fast", "full")

    _write_results(ws, [
        {"scan": 2, "detector": "det", "h": 2.2, "k": 0.0, "l": 0.0,
         "status": "fitted", "disposition": "flagged", "omega0": 1.05,
         "hesse_lo": 1.04, "hesse_hi": 1.06, "hesse_trusted": False},
        {"scan": 9000001, "detector": "merged", "h": 2.2, "k": 0.0, "l": 0.0,
         "status": "fitted", "disposition": "resolved", "omega0": 1.05,
         "pooled": True, "hesse_lo": 1.04, "hesse_hi": 1.06,
         "hesse_trusted": True},
    ])
    assert _errors_tiers_for(_prop(2.2), ws) == ("fast", "fast")


def test_full_tier_helper():
    assert _full_tier(None) == "full"
    assert _full_tier(types.SimpleNamespace(bayes_errors=False)) == "full"
    assert _full_tier(types.SimpleNamespace(bayes_errors=True)) == "bayes"


def test_errors_tier_ladder_with_bayes_errors_flag_escalates_to_bayes(tmp_path):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    cfg = types.SimpleNamespace(bayes_errors=True)
    # Quick-looks stay cheap regardless of the flag.
    assert _errors_tier_for(_prop(2.1, "quicklook"), ws, cfg) == "cheap"
    # No prior fit at this q → production still runs fast.
    assert _errors_tier_for(_prop(2.1), ws, cfg) == "fast"
    # A usable fit whose only error is an untrusted HESSE → escalate to "bayes" (not "full") when opted in.
    _write_results(ws, [{"scan": 1, "detector": "det", "h": 2.1, "k": 0.0, "l": 0.0,
                         "status": "fitted", "disposition": "flagged", "omega0": 0.95,
                         "pooled": True, "hesse_lo": 0.94, "hesse_hi": 0.96,
                         "hesse_trusted": False}])
    assert _errors_tier_for(_prop(2.1), ws, cfg) == "bayes"
    assert _errors_arg_for(_prop(2.1), ws, cfg) == ["--errors", "bayes"]
    # A trusted-HESSE q stays on the cheap fast tier, same as without the flag.
    _write_results(ws, [{"scan": 2, "detector": "det", "h": 2.2, "k": 0.0, "l": 0.0,
                         "status": "fitted", "disposition": "resolved", "omega0": 1.1,
                         "pooled": True, "hesse_lo": 1.09, "hesse_hi": 1.11,
                         "hesse_trusted": True}])
    assert _errors_tier_for(_prop(2.2), ws, cfg) == "fast"


def test_errors_tier_ladder_flag_off_is_byte_identical_to_no_cfg(tmp_path):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    cfg_off = types.SimpleNamespace(bayes_errors=False)
    assert _errors_tier_for(_prop(2.1, "quicklook"), ws, cfg_off) == \
        _errors_tier_for(_prop(2.1, "quicklook"), ws, None) == "cheap"
    assert _errors_tier_for(_prop(2.1), ws, cfg_off) == _errors_tier_for(_prop(2.1), ws, None) == "fast"
    _write_results(ws, [{"scan": 1, "detector": "det", "h": 2.1, "k": 0.0, "l": 0.0,
                         "status": "fitted", "disposition": "flagged", "omega0": 0.95,
                         "pooled": True, "hesse_lo": 0.94, "hesse_hi": 0.96,
                         "hesse_trusted": False}])
    assert _errors_tier_for(_prop(2.1), ws, cfg_off) == _errors_tier_for(_prop(2.1), ws, None) == "full"


def test_campaign_config_bayes_errors_flag_default_and_override(tmp_path):
    ws1, ws2 = tmp_path / "ws1", tmp_path / "ws2"
    (ws1 / "report").mkdir(parents=True)
    (ws2 / "report").mkdir(parents=True)
    base = {"client": MockTavi(lambda h: 0.0), "run_isar": lambda *a, **k: None,
            "calibration": _UNITY_CAL}
    assert _campaign_config(_objective(), dict(base, workspace=ws1)).bayes_errors is False
    assert _campaign_config(_objective(), dict(base, workspace=ws2, bayes_errors=True)).bayes_errors is True


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


def test_campaign_certify_pass_uses_bayes_tier_when_flag_set(tmp_path):
    # Same convergence path as test_campaign_reaches_met_via_hesse_then_certifies, but with the
    # bayes_errors campaign flag on: the certify pass (and any in-loop full-tier escalation) must run
    # with errors_tier="bayes", not "full".
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    tiers_seen = []
    base_runner = make_mock_runner(omega, hesse_only=True)

    def recording_runner(workspace, data_paths, extra_args=None, **kw):
        # run_isar is a bare callable: the runner seam (as_runner) reconstructs the structured
        # errors_tier into `--errors <tier>` extra_args, same shape isar_io.runner.SubprocessRunner
        # emits — see test_as_runner_wraps_bare_callable_reconstructs_fast_tier.
        extra_args = list(extra_args or [])
        if "--errors" in extra_args:
            tiers_seen.append(extra_args[extra_args.index("--errors") + 1])
        return base_runner(workspace, data_paths, **kw)

    summary, ws = _run(tmp_path, _objective(), client, recording_runner, bayes_errors=True)
    assert summary["stop"]["state"] == "met", summary["stop"]["reasons"]
    events = read_log(summary["session_log"])
    esc = [(e.get("payload") or {}) for e in events if e["kind"] == "escalate_fit"]
    certify_start = [(e.get("payload") or {}) for e in events
                     if e["kind"] == "decide" and (e.get("payload") or {}).get("event") == "certify_start"]
    assert certify_start and certify_start[0]["tier"] == "bayes"
    for p in esc:
        if p.get("to_tier") is not None:
            assert p["to_tier"] != "full"
    assert "bayes" in tiers_seen
    assert "full" not in tiers_seen


def _objective(tol=0.5, amount=1e13):
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=tol,
                                 budget={"kind": "neutrons", "amount": amount}, name="mvp")


def _run(tmp_path, objective, client, runner, **extra):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True, exist_ok=True)
    cfg = {"workspace": ws, "client": client, "run_isar": runner, "calibration": _UNITY_CAL,
           "resolution": {"fwhm_dE": 0.8}, "seed": 7, **extra}
    return run_campaign(objective, cfg), ws


def test_terminal_and_certification_refuse_unresolved_current_promotion(tmp_path, monkeypatch):
    _no_subprocess(monkeypatch)
    pending = {"promotion_id": "promotion-current", "status": "pending"}
    monkeypatch.setattr(loop_mod, "_unresolved_current_promotions", lambda _workspace: [pending])
    omega = lambda h: (h - 2.0) * 2.0

    summary, _ws = _run(
        tmp_path, _objective(), MockTavi(omega), make_mock_runner(omega, hesse_only=True))

    assert summary["stop"]["state"] == "incomplete"
    assert "promotion-current" in summary["stop"]["reasons"][0]
    events = read_log(summary["session_log"])
    assert any(event["kind"] == "recovery_pending"
               and (event.get("payload") or {}).get("action") == "master_promotion_pending"
               for event in events)
    assert not any((event.get("payload") or {}).get("event") == "certify_start"
                   for event in events if event["kind"] == "decide")


def _widen_last_pooled_master(workspace, halfwidth=0.75):
    results_path = Path(workspace) / "report" / "results.csv"
    with open(results_path, newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
        fieldnames = list(rows[0])
    pooled_rows = [row for row in rows
                   if str(row.get("pooled", "")).lower() in {"true", "1"}]
    failed_h = max(float(row["h"]) for row in pooled_rows)
    for row in pooled_rows:
        if float(row["h"]) != failed_h:
            continue
        center = float(row["omega0"])
        for prefix in ("interval", "hesse", "model_interval"):
            lo_key, hi_key = f"{prefix}_lo", f"{prefix}_hi"
            if lo_key in row:
                row[lo_key] = str(center - halfwidth)
            if hi_key in row:
                row[hi_key] = str(center + halfwidth)
    with open(results_path, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


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
    assert summary["session_log_relative"] == "report/session_log.jsonl"


def test_certification_regression_automatically_acquires_and_recertifies(
        tmp_path, monkeypatch):
    _no_subprocess(monkeypatch)
    omega = lambda h: (h - 2.0) * 2.0
    runner = make_mock_runner(omega)

    certification_calls = []

    def widen_first_certification(*, cfg, objective, decision, observations, scan_kind):
        certification_calls.append([dict(point) for point in decision.per_q])
        if len(certification_calls) > 1:
            return observations, True
        _widen_last_pooled_master(cfg.workspace)
        return observations, True

    monkeypatch.setattr(loop_mod, "_certify", widen_first_certification)
    monkeypatch.setattr(loop_mod, "_predictive_escalation_candidate", lambda *_args: None)
    summary, ws = _run(tmp_path, _objective(), MockTavi(omega), runner)

    assert summary["stop"]["state"] == "met"
    assert len(certification_calls) == 2
    events = read_log(summary["session_log"])
    regression = [event for event in events
                  if (event.get("payload") or {}).get("action") == "certify_regression"]
    assert len(regression) == 1 and regression[0]["payload"]["state"] == "continue"
    assert regression[0]["payload"]["automatic_resume"] is True
    regression_position = events.index(regression[0])
    assert any(event["kind"] == "propose" for event in events[regression_position + 1:])
    assert any(event["kind"] == "run" for event in events[regression_position + 1:])
    assert events[-1]["kind"] == "stop"
    assert events[-1]["payload"]["state"] == "met"


def test_certification_retry_stops_when_provisional_basis_did_not_change(
        tmp_path, monkeypatch):
    _no_subprocess(monkeypatch)
    omega = lambda h: (h - 2.0) * 2.0
    certification_calls = []

    def always_regress(*, cfg, objective, decision, observations, scan_kind):
        certification_calls.append(1)
        _widen_last_pooled_master(cfg.workspace)
        return observations, True

    monkeypatch.setattr(loop_mod, "_certify", always_regress)
    summary, _ws = _run(
        tmp_path, _objective(), MockTavi(omega), make_mock_runner(omega))

    # One predictive full-fit escalation legitimately changes the basis and permits a second check;
    # the unchanged second basis is then stopped rather than spinning through max_iters.
    assert len(certification_calls) == 2
    assert summary["stop"]["state"] == "incomplete"
    assert "without a changed scientific state" in summary["stop"]["reasons"][0]


def test_operator_stop_during_regressed_certification_prevents_corrective_scan(
        tmp_path, monkeypatch):
    _no_subprocess(monkeypatch)
    omega = lambda h: (h - 2.0) * 2.0
    token = "stop-during-certification"

    def regress_and_request_stop(*, cfg, objective, decision, observations, scan_kind):
        _widen_last_pooled_master(cfg.workspace)
        control = cfg.workspace / "report" / "control"
        control.mkdir(parents=True, exist_ok=True)
        (control / f"stop-{token}.json").write_text("{}", encoding="utf-8")
        return observations, True

    monkeypatch.setattr(loop_mod, "_certify", regress_and_request_stop)
    summary, _ws = _run(
        tmp_path, _objective(), MockTavi(omega), make_mock_runner(omega),
        control_token=token)
    events = read_log(summary["session_log"])
    regression_index = next(index for index, event in enumerate(events)
                            if (event.get("payload") or {}).get("action")
                            == "certify_regression")

    assert summary["stop"]["state"] == "operator_stopped"
    assert not any(event["kind"] == "run" for event in events[regression_index + 1:])
    stops = [event for event in events if event["kind"] == "stop"]
    assert len(stops) == 1 and stops[0]["payload"]["state"] == "operator_stopped"


def test_certification_acceptance_selects_only_failed_or_changed_revisions(tmp_path):
    cfg = types.SimpleNamespace(
        workspace=tmp_path, log_path=tmp_path / "report" / "session_log.jsonl",
        calibration=types.SimpleNamespace(inflation=1.0), floor=0.0)
    first = StopDecision("continue", [], [
        {"hkl": [2.1, 0.0, 0.0], "converged": True, "master_revision": "rev-a",
         "calibrated_halfwidth": 0.08, "tolerance": 0.1},
        {"hkl": [2.2, 0.0, 0.0], "converged": False, "master_revision": "rev-b",
         "calibrated_halfwidth": 0.12, "tolerance": 0.1},
    ])
    _record_certification_acceptance(cfg, first, "full")

    retry = StopDecision("met", [], [
        {**first.per_q[0], "converged": True},
        {**first.per_q[1], "converged": True, "master_revision": "rev-c"},
    ])
    assert _pending_certification_indices(cfg, retry, "full") == [1]

    retry.per_q[0]["master_revision"] = "rev-a2"
    assert _pending_certification_indices(cfg, retry, "full") == [0, 1]
    assert _pending_certification_indices(cfg, retry, "bayes") == [0, 1]

    retry.per_q[0]["master_revision"] = "rev-a"
    cfg.calibration.inflation = 1.1
    assert _pending_certification_indices(cfg, retry, "full") == [0, 1]
    cfg.calibration.inflation = 1.0
    (tmp_path / "card.state.json").write_text(json.dumps({
        "resolution_versions": [{"version": "new-resolution"}],
        "energy_zero_series": [], "hypothesis_snapshot": None, "campaign_target": None,
    }), encoding="utf-8")
    assert _pending_certification_indices(cfg, retry, "full") == [0, 1]


def test_certify_sends_only_dirty_q_in_master_worklist(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    cfg_base = types.SimpleNamespace(
        workspace=workspace, log_path=workspace / "report" / "session_log.jsonl",
        calibration=types.SimpleNamespace(inflation=1.0), floor=0.0)
    accepted = StopDecision("continue", [], [
        {"hkl": [2.1, 0.0, 0.0], "converged": True, "master_revision": "rev-a",
         "calibrated_halfwidth": 0.08, "tolerance": 0.1},
        {"hkl": [2.2, 0.0, 0.0], "converged": False, "master_revision": "rev-b",
         "calibrated_halfwidth": 0.12, "tolerance": 0.1},
    ])
    _record_certification_acceptance(cfg_base, accepted, "full")
    requests = []

    class Runner:
        supports_master_certification = True

        def run(self, _workspace, _data_paths, **kwargs):
            request = json.loads(
                Path(kwargs["certification_request"]).read_text(encoding="utf-8"))
            requests.append(request)
            return types.SimpleNamespace(completed=True)

    cfg = types.SimpleNamespace(
        workspace=workspace, data_dir=workspace / "data", log_path=cfg_base.log_path,
        calibration=types.SimpleNamespace(inflation=1.0), floor=0.0,
        runner=Runner(), bayes_errors=False)
    objective = types.SimpleNamespace(
        path=[(2.1, 0.0, 0.0), (2.2, 0.0, 0.0)], analysis_limit_seconds=None)
    decision = StopDecision("met", [], [
        {**accepted.per_q[0], "converged": True},
        {**accepted.per_q[1], "converged": True, "master_revision": "rev-c"},
    ])
    monkeypatch.setattr(loop_mod, "_verify_certification_publication",
                        lambda request_path, _workspace: request_path)

    _observations, ran = loop_mod._certify(
        cfg=cfg, objective=objective, decision=decision, observations=[], scan_kind={})

    assert ran and len(requests) == 1
    assert requests[0]["objective_path"] == [[2.2, 0.0, 0.0]]
    # per_q additionally carries the stopping decision's coverage verdict, so certification can tell
    # a structurally unresolvable q from one whose pooled master is genuinely missing.
    assert requests[0]["per_q"] == [{
        "hkl": [2.2, 0.0, 0.0], "objective_index": 1,
        "precision_coverage_eligible": True, "structural_endpoint_status": None,
        "is_reduced_q_origin": False, "reason": None}]
    assert requests[0]["runtime"]["total"] == 1


def test_optic_campaign_tracks_target_amid_acoustic_distractor_deterministically(
        tmp_path, monkeypatch):
    """End-to-end steering regression for the failure that motivated branch-aware campaigns."""
    _no_subprocess(monkeypatch)

    def execute(root):
        frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
        frozen["steering"] = {**frozen["steering"],
                              "differentiation_tolerance_meV": 1.2,
                              "quicklook_ncount": 50_000_000,
                              "production_batch_ncount": 100_000_000}
        frozen["hypothesis_fingerprint"] = content_fingerprint(
            frozen["hypotheses"], frozen["steering"])
        objective = ObjectiveSpec.segment(
            q_start=(2.0, 0.0, 0.0), q_end=(2.0, 0.5, 0.0), step_rlu=0.1,
            tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 2.0e12},
            name="optic-two-branch", target_branch="optic", target_label="transverse optic",
            energy_search_meV=(-2.0, 12.0), **frozen)
        client = BranchAwareOpticTavi(lambda _h: 0.0)
        summary, workspace = _run(
            root, objective, client, make_branch_aware_optic_runner(objective), max_iters=60)
        events = read_log(summary["session_log"])
        proposals = [event["payload"] for event in events if event["kind"] == "propose"]
        return summary, proposals, workspace

    first_summary, first_proposals, first_workspace = execute(tmp_path / "first")
    second_summary, second_proposals, _ = execute(tmp_path / "second")

    # Optic campaigns begin at Γ and never waste the first scan on the Bragg/elastic position.
    first = first_proposals[0]
    assert first["hkl"] == [2.0, 0.0, 0.0]
    assert first["e_lo"] > 0.0 and first["e_lo"] < 6.0 < first["e_hi"]
    assert first["ncount"] == 50_000_000
    assert any(proposal["kind"] == "production" and proposal["ncount"] >= 100_000_000
               for proposal in first_proposals)

    # The resolved dispersion stays on the ~6 meV optic branch; the strong acoustic distractor rises
    # from zero to 6 meV but never becomes the reported target (no acoustic/optic sawtooth).
    energies = [row["omega0"] for row in first_summary["dispersion"]]
    assert len(energies) == 6 and min(energies) >= 5.9
    assert all(abs(right - left) < 0.2 for left, right in zip(energies, energies[1:]))
    with open(first_workspace / "report" / "results.csv", newline="", encoding="utf-8") as handle:
        result_rows = list(csv.DictReader(handle))
    assert any(row["branch_id"] == "acoustic" and row["disposition"] == "resolved"
               for row in result_rows)
    assert any(float(row["omega0"] or 0.0) < 5.0 for row in result_rows
               if row["branch_id"] == "acoustic")

    # Once requested points are seeded, at least one explicitly non-certifying probe wins on branch
    # information and exposes the MI in its score; rival support also reaches the scan window.
    inserted = [proposal for proposal in first_proposals
                if proposal["meta"].get("driver_inserted")]
    assert inserted
    assert all(not proposal["meta"]["certification_eligible"] for proposal in inserted)
    assert any((proposal["meta"]["score"].get("classification_eig") or 0.0) > 0.0
               for proposal in inserted)
    assert any(proposal["meta"].get("rival_band") is not None for proposal in first_proposals[1:])

    # Branch beliefs survive into the human/machine terminal artifact, and terminal success is honest:
    # every requested point—not a diagnostic probe—is converged.
    assert {row["branch_id"] for row in first_summary["branch_beliefs"]} == {"optic", "acoustic"}
    assert first_summary["stop"]["state"] == "met"
    assert all(row["converged"] for row in first_summary["grid"])
    final_events = read_log(first_summary["session_log"])
    assert len([event for event in final_events
                if event["kind"] == "certification_crossing"]) == len(first_summary["grid"])
    assert not any(event["kind"] == "exception"
                   and (event.get("payload") or {}).get("action") == "certify_regression"
                   for event in final_events)
    persisted = json.loads(
        (first_workspace / "report" / "campaign_summary.json").read_text(encoding="utf-8"))
    assert persisted["stop"]["state"] == "met" and persisted["branch_beliefs"]

    # Fixed objective, spectra, priors, and seed produce the same acquisition sequence on a fresh run.
    signature = lambda proposals: [
        (tuple(proposal["hkl"]), proposal["kind"], proposal["meta"].get("driver_inserted"),
         round(proposal["e_lo"], 8), round(proposal["e_hi"], 8))
        for proposal in proposals]
    assert signature(second_proposals) == signature(first_proposals)
    assert second_summary["stop"]["state"] == first_summary["stop"]["state"]


def test_optic_campaign_with_supplied_energy_runs_nearest_selector(tmp_path, monkeypatch):
    """The scientist-facing supplied-energy mode traverses the complete campaign loop."""
    _no_subprocess(monkeypatch)
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    frozen["steering"] = {**frozen["steering"],
                          "quicklook_ncount": 50_000_000,
                          "production_batch_ncount": 100_000_000}
    frozen["hypothesis_fingerprint"] = content_fingerprint(
        frozen["hypotheses"], frozen["steering"])
    objective = ObjectiveSpec.segment(
        q_start=(2.0, 0.0, 0.0), q_end=(2.0, 0.2, 0.0), step_rlu=0.1,
        tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1.0e12},
        name="optic-nearest", target_branch="optic", target_label="transverse optic",
        target_energy_meV=6.1, energy_search_meV=(-2.0, 12.0), **frozen)
    assert objective.target_selector == "nearest_energy"

    summary, _workspace = _run(
        tmp_path, objective, BranchAwareOpticTavi(lambda _h: 0.0),
        make_branch_aware_optic_runner(objective), max_iters=40)
    proposals = [event["payload"] for event in read_log(summary["session_log"])
                 if event["kind"] == "propose"]
    assert proposals[0]["e_lo"] < 6.1 < proposals[0]["e_hi"]
    assert proposals[0]["ncount"] == 50_000_000
    assert summary["stop"]["state"] == "met"
    assert all(abs(row["omega0"] - 6.1) <= 0.2 for row in summary["dispersion"])


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


def test_campaign_incomplete_on_budget(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(amount=1.0), client, make_mock_runner(omega))
    assert summary["stop"]["state"] == "incomplete"
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
    submissions = [e["payload"] for e in events if e["kind"] == "physical_submission"]
    assert submissions
    by_acquisition = {}
    for submission in submissions:
        by_acquisition.setdefault(submission["acquisition_id"], []).append(submission)
    retried = next(group for group in by_acquisition.values() if len(group) > 1)
    assert [item["attempt"] for item in retried[:2]] == [0, 1]
    assert all(item["submitted_ncount"] == item["ncount"] for item in submissions)
    state = json.loads((ws / "card.state.json").read_text(encoding="utf-8"))
    provenance = state["acquisition_provenance"]
    assert {item["job_id"] for item in provenance} == {item["job_id"] for item in submissions}
    by_job = {item["job_id"]: item for item in provenance}
    for submission in submissions:
        saved = by_job[submission["job_id"]]
        assert saved["acquisition_id"] == submission["acquisition_id"]
        assert saved["submitted_ncount"] == submission["submitted_ncount"]
        assert saved["scan_kind"] == submission["kind"]


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
    inloop = [p for p in promotes if p["payload"].get("iter") is not None
              and p["payload"].get("reason") == "untrusted_hesse_no_reliable_interval"]
    assert inloop
    for p in inloop:
        assert p["payload"]["to_tier"] == "full"
        assert p["payload"]["reason"] == "untrusted_hesse_no_reliable_interval"
    # Each in-loop promotion is paired with exactly one outcome at the same iteration, carrying a valid
    # enum and a job_id that traces back to a real run event.
    run_job_ids = {(e["payload"] or {}).get("job_id") for e in events if e["kind"] == "run"}
    paired_outcomes = [o for o in outcomes if o["payload"].get("job_id")]
    assert len(paired_outcomes) == len(inloop)
    for o in paired_outcomes:
        assert o["payload"]["outcome"] in ("seated", "failed", "superseded")
        assert o["payload"]["job_id"] in run_job_ids
        assert any(p["payload"].get("iter") == o["payload"]["iter"] for p in inloop)
    # Every decide event carries the additive `action` field mirroring its `event`.
    decides = [e["payload"] for e in events if e["kind"] == "decide"]
    assert decides
    for d in decides:
        assert d.get("action") == d.get("event")


def test_bayes_master_promotion_logs_actual_tier_and_outcome(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    summary, _ws = _run(
        tmp_path, _objective(), MockTavi(omega), make_untrusted_hesse_runner(omega),
        max_iters=80, bayes_errors=True)
    events = read_log(summary["session_log"])
    promoted = [event for event in events if event["kind"] == "escalate_fit"
                and (event.get("payload") or {}).get("phase") == "promote"
                and (event.get("payload") or {}).get("iter") is not None]
    outcomes = [event for event in events if event["kind"] == "escalate_fit"
                and (event.get("payload") or {}).get("phase") == "outcome"
                and (event.get("payload") or {}).get("iter") is not None]
    assert promoted
    assert len(outcomes) == len(promoted)
    assert all(event["payload"]["to_tier"] == "bayes" for event in promoted + outcomes)


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


def test_detailed_resume_replays_changing_ncount_and_master_updates_chronologically(tmp_path):
    ws = tmp_path / "ws-detailed"
    (ws / "report").mkdir(parents=True)
    log = ws / "report" / "session_log.jsonl"
    for index, ncount in enumerate((10_000_000, 40_000_000, 160_000_000), start=1):
        submission = {"job_id": f"j{index}", "state": "done",
                      "eta": {"estimated_seconds": float(index)},
                      "kind": "quicklook" if index == 1 else "production",
                      "ncount": ncount, "submitted_ncount": ncount, "n_points": 10,
                      "hkl": [2.1, 0.0, 0.0], "cost": 10.0 * ncount,
                      "acquisition_id": f"A{index}", "driver_inserted": False,
                      "certification_eligible": True}
        log_event(log, "run", submission)
        log_event(log, "physical_submission", submission)
        log_event(log, "pooled_master_update", {
            "kind": "pooled_master_update", "hkl": [2.1, 0.0, 0.0],
            "acquisition_id": f"A{index}", "candidate_id": "optic:0.1:lowest_energy",
            "branch_id": "optic", "interval_method": "profile",
            "exposure_bucket": "global", "calibration_version": "v1",
            "effective_exposure": float(ncount), "diagnostic_halfwidth": 1.0 / index})
    base = {"h": 2.1, "k": 0.0, "l": 0.0, "status": "fitted",
            "verdict": "resolved", "disposition": "resolved", "omega0": 6.0,
            "interval_lo": 5.8, "interval_hi": 6.2, "interval_reliable": True}
    _write_results(ws, [{"scan": scan, "detector": "det", **base} for scan in (1, 2, 3)])

    resumed = _resume_state(ws, log, _UNITY_CAL)
    physical = [item for item in resumed["observations"]
                if item.get("kind") != "pooled_master_update"]
    assert [item["submitted_ncount"] for item in physical] == [
        10_000_000, 40_000_000, 160_000_000]
    assert [item["acquisition_id"] for item in physical] == ["A1", "A2", "A3"]
    assert [item["proposal_cost"] for item in physical] == [
        100_000_000.0, 400_000_000.0, 1_600_000_000.0]
    assert [item.get("kind") for item in resumed["observations"]] == [
        "quicklook", "pooled_master_update", "production", "pooled_master_update",
        "production", "pooled_master_update"]
    assert resumed["scan_counter"] == 3


def test_detailed_resume_reconstructs_pre_provenance_live_ncount_clamp(tmp_path):
    ws = tmp_path / "ws-clamp-replay"
    (ws / "report").mkdir(parents=True)
    log = ws / "report" / "session_log.jsonl"
    log_event(log, "exception", {
        "action": "proposal_ncount_clamped_to_limit", "iter": 4,
        "hkl": [2.3, 0.0, 0.0], "requested_ncount": 400_000_000,
        "effective_ncount": 100_000_000,
    })
    submission = {
        "job_id": "j-clamped", "state": "done", "iter": 4,
        "kind": "production", "ncount": 100_000_000,
        "submitted_ncount": 100_000_000, "n_points": 20,
        "hkl": [2.3, 0.0, 0.0], "cost": 2_000_000_000.0,
        "acquisition_id": "A-clamped",
    }
    log_event(log, "run", submission)
    log_event(log, "physical_submission", submission)
    log_event(log, "analyze", {"job_id": "j-clamped", "n_rows": 1})
    _write_results(ws, [{
        "scan": 1, "detector": "det", "h": 2.3, "k": 0.0, "l": 0.0,
        "status": "fitted", "verdict": "branch_ambiguous",
        "disposition": "flagged", "omega0": 2.7,
        "interval_lo": 2.5, "interval_hi": 2.9, "interval_reliable": True,
    }])
    resumed = _resume_state(ws, log, _UNITY_CAL)
    physical = [row for row in resumed["observations"]
                if row.get("kind") != "pooled_master_update"]
    assert len(physical) == 1
    assert physical[0]["ncount_clamped"] is True
    assert physical[0]["requested_ncount"] == 400_000_000
    assert physical[0]["runtime_ncount_limit"] == 100_000_000


def test_detailed_resume_keeps_legacy_prefix_and_charges_multidetector_scan_once(tmp_path):
    ws = tmp_path / "ws-mixed-ledger"
    (ws / "report").mkdir(parents=True)
    log = ws / "report" / "session_log.jsonl"
    legacy = {"job_id": "old", "state": "done", "kind": "quicklook",
              "ncount": 10_000_000, "n_points": 10, "hkl": [2.1, 0.0, 0.0],
              "cost": 100_000_000.0, "eta": {"estimated_seconds": 1.0}}
    detailed = {"job_id": "new", "state": "done", "kind": "production",
                "ncount": 40_000_000, "submitted_ncount": 40_000_000, "n_points": 10,
                "hkl": [2.1, 0.0, 0.0], "cost": 400_000_000.0,
                "eta": {"estimated_seconds": 2.0}, "acquisition_id": "A2"}
    log_event(log, "run", legacy)
    log_event(log, "run", detailed)
    log_event(log, "physical_submission", detailed)
    base = {"h": 2.1, "k": 0.0, "l": 0.0, "status": "fitted",
            "verdict": "resolved", "disposition": "resolved", "omega0": 6.0,
            "interval_lo": 5.8, "interval_hi": 6.2, "interval_reliable": True}
    _write_results(ws, [
        {"scan": 1, "detector": "d1", **base},
        {"scan": 2, "detector": "d1", **base},
        {"scan": 2, "detector": "d2", **base},
    ])
    resumed = _resume_state(ws, log, _UNITY_CAL)
    assert resumed["scan_counter"] == 2
    assert resumed["neutrons"] == pytest.approx(500_000_000.0)
    assert resumed["seconds"] == pytest.approx(3.0)
    new_rows = [row for row in resumed["observations"] if row.get("acquisition_id") == "A2"]
    assert len(new_rows) == 2
    assert sum(row["proposal_cost"] for row in new_rows) == pytest.approx(400_000_000.0)
    assert summarize_grid(_objective(), resumed["observations"])[0].n_production == 1


def test_resume_state_restores_driver_inserted_certification_provenance(tmp_path):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    log = ws / "report" / "session_log.jsonl"
    log_event(log, "run", {
        "job_id": "probe", "state": "done", "eta": {"estimated_seconds": 1.0},
        "kind": "quicklook", "ncount": QUICKLOOK_NCOUNT, "n_points": 20,
        "hkl": [2.15, 0.0, 0.0], "cost": 2.0e8,
        "driver_inserted": True, "certification_eligible": False,
        "insertion_purpose": "differentiate",
    })
    _write_results(ws, [{"scan": 1, "detector": "det", "h": 2.15, "k": 0.0, "l": 0.0,
                         "status": "fitted", "verdict": "resolved", "disposition": "resolved",
                         "omega0": 6.0, "interval_lo": 5.8, "interval_hi": 6.2,
                         "interval_reliable": True}])
    resumed = _resume_state(ws, log, _UNITY_CAL)
    assert len(resumed["observations"]) == 1
    observation = resumed["observations"][0]
    assert observation["driver_inserted"] is True
    assert observation["certification_eligible"] is False


def test_declined_inserted_observation_keeps_noncertification_provenance():
    proposal = _scan_proposal(1.0, 2.0, 0.1)
    proposal.meta.update({"driver_inserted": True, "certification_eligible": False,
                          "insertion_purpose": "differentiate"})
    observation = _declined_observation(proposal, "validation_rejected")
    assert observation["driver_inserted"] is True
    assert observation["certification_eligible"] is False


def test_resume_keeps_nearby_probe_provenance_ledgers_distinct(tmp_path):
    ws = tmp_path / "ws-nearby"
    (ws / "report").mkdir(parents=True)
    log = ws / "report" / "session_log.jsonl"
    for job_id, h, inserted in (("probe", 2.15001, True), ("grid", 2.15004, False)):
        log_event(log, "run", {
            "job_id": job_id, "state": "done", "kind": "quicklook",
            "ncount": QUICKLOOK_NCOUNT, "n_points": 20, "hkl": [h, 0.0, 0.0],
            "cost": 2.0e8, "driver_inserted": inserted,
            "certification_eligible": not inserted,
        })
    base = {"detector": "det", "k": 0.0, "l": 0.0, "status": "fitted",
            "verdict": "resolved", "disposition": "resolved", "omega0": 6.0,
            "interval_lo": 5.8, "interval_hi": 6.2, "interval_reliable": True}
    _write_results(ws, [{"scan": 1, "h": 2.15001, **base},
                        {"scan": 2, "h": 2.15004, **base}])
    observations = _resume_state(ws, log, _UNITY_CAL)["observations"]
    by_h = {round(observation["hkl"][0], 5): observation for observation in observations}
    assert by_h[2.15001]["driver_inserted"] is True
    assert by_h[2.15001]["certification_eligible"] is False
    assert by_h[2.15004]["driver_inserted"] is False
    assert by_h[2.15004]["certification_eligible"] is True


def test_cold_workspace_resumes_to_fresh(tmp_path):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    r = _resume_state(ws, ws / "report" / "session_log.jsonl", _UNITY_CAL)
    assert r == {"observations": [], "consumed": set(), "scan_kind": {},
                 "scan_counter": 0, "neutrons": 0.0, "seconds": 0.0,
                 "proposal_counter": 0,
                 "unmatched_submissions": []}


def _seed_unmatched_submission(ws, *, job_id="old-job", state="queued"):
    (ws / "report").mkdir(parents=True, exist_ok=True)
    ncount = PRODUCTION_NCOUNT
    payload = {
        "job_id": job_id, "state": state, "eta": {"estimated_seconds": 5.0},
        "kind": "production", "ncount": ncount, "submitted_ncount": ncount,
        "requested_points": 21, "n_points": 21, "hkl": [2.1, 0.0, 0.0],
        "cost": 21.0 * ncount, "acquisition_id": "A-recover",
        "acquisition_target_energy": 0.2, "driver_inserted": False,
        "certification_eligible": True,
    }
    log = ws / "report" / "session_log.jsonl"
    log_event(log, "run", payload)
    log_event(log, "physical_submission", payload)
    body = {"parameters": {"H": 2.1, "K": 0.0, "L": 0.0,
                           "scan_command1": "deltaE -0.8 1.2 0.1",
                           "number_neutrons": ncount, "sample": "Al_phonon_DFT"}}
    return payload, body


class _RecoveryTavi(MockTavi):
    def __init__(self, body, *, state="done", wait_state=None, unreachable=False):
        super().__init__(lambda _h: 0.2)
        self.jobs = {"old-job": body}
        self.state = state
        self.wait_state = wait_state if wait_state is not None else state
        self.unreachable = unreachable
        self.submit_calls = 0
        self.wait_calls = 0

    def submit_scan(self, body, idempotency_key=None):
        self.submit_calls += 1
        return super().submit_scan(body, idempotency_key=idempotency_key)

    def get_scan(self, job_id):
        if self.unreachable:
            raise RuntimeError("instrument offline")
        return {"job_id": job_id, "state": self.state}

    def wait_scan(self, job_id, timeout=0.0, **kw):
        self.wait_calls += 1
        return {"job_id": job_id, "state": self.wait_state}


def _recovery_config(ws, client, omega, *, max_iters=0):
    return {"workspace": ws, "client": client, "run_isar": make_mock_runner(omega),
            "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8},
            "seed": 7, "max_iters": max_iters}


def test_resume_reconstructs_analyzed_zero_rows_and_terminal_failure(tmp_path):
    for suffix, event, expected in [
        ("zero", ("analyze", {"job_id": "old-job", "n_rows": 0}), "analysis_no_result"),
        ("failed", ("exception", {"action": "scan_not_done", "job_id": "old-job",
                                   "state": "failed"}), "failed"),
    ]:
        ws = tmp_path / suffix
        _seed_unmatched_submission(ws)
        log_event(ws / "report" / "session_log.jsonl", event[0], event[1])
        resumed = _resume_state(ws, ws / "report" / "session_log.jsonl", _UNITY_CAL)
        assert resumed["unmatched_submissions"] == []
        assert len(resumed["observations"]) == 1
        assert resumed["observations"][0]["verdict"] == expected
        assert resumed["observations"][0]["acquisition_id"] == "A-recover"


def test_resume_keeps_latest_same_q_submission_unmatched(tmp_path):
    ws = tmp_path / "same-q"
    first, _body = _seed_unmatched_submission(ws, job_id="j1")
    log = ws / "report" / "session_log.jsonl"
    log_event(log, "analyze", {"job_id": "j1", "n_rows": 1})
    second = {**first, "job_id": "j2", "acquisition_id": "A2"}
    log_event(log, "run", second)
    log_event(log, "physical_submission", second)
    _write_results(ws, [{"scan": 1, "detector": "det", "h": 2.1, "k": 0.0, "l": 0.0,
                         "status": "fitted", "verdict": "resolved", "disposition": "resolved",
                         "omega0": 0.2, "interval_lo": 0.05, "interval_hi": 0.35,
                         "interval_reliable": True}])
    resumed = _resume_state(ws, log, _UNITY_CAL)
    assert [item["payload"]["job_id"] for item in resumed["unmatched_submissions"]] == ["j2"]
    physical = [item for item in resumed["observations"]
                if item.get("kind") != "pooled_master_update"]
    assert len(physical) == 1 and physical[0]["acquisition_id"] == "A-recover"


def test_resume_recovers_remote_completed_job_without_resubmission(tmp_path, monkeypatch):
    _no_subprocess(monkeypatch)
    ws = tmp_path / "remote"
    _payload, body = _seed_unmatched_submission(ws)
    client = _RecoveryTavi(body, state="done")
    summary = run_campaign(
        _objective(), _recovery_config(ws, client, lambda _h: 0.2))
    events = read_log(summary["session_log"])
    assert summary["stop"]["state"] != "pending_recovery"
    assert client.submit_calls == 0 and client.wait_calls == 0
    assert (ws / "data" / "old-job.json").exists()
    assert any(event["kind"] == "recovery_completed" for event in events)
    assert any(event["kind"] == "analyze"
               and event["payload"].get("job_id") == "old-job" for event in events)


def test_resume_reuses_saved_raw_job_without_contact_or_resubmission(tmp_path, monkeypatch):
    _no_subprocess(monkeypatch)
    ws = tmp_path / "raw"
    _payload, body = _seed_unmatched_submission(ws)
    client = _RecoveryTavi(body, unreachable=True)
    save_scan_json(client.get_scan_data("old-job"), ws / "data")
    summary = run_campaign(
        _objective(), _recovery_config(ws, client, lambda _h: 0.2))
    assert summary["stop"]["state"] != "pending_recovery"
    assert client.submit_calls == 0 and client.wait_calls == 0


@pytest.mark.parametrize("unreachable,state,wait_state", [
    (False, "running", "running"),
    (True, "queued", None),
])
def test_resume_pending_recovery_never_submits_new_scan(
        tmp_path, monkeypatch, unreachable, state, wait_state):
    _no_subprocess(monkeypatch)
    ws = tmp_path / ("offline" if unreachable else "running")
    _payload, body = _seed_unmatched_submission(ws)
    client = _RecoveryTavi(
        body, state=state, wait_state=wait_state, unreachable=unreachable)
    summary = run_campaign(
        _objective(), _recovery_config(ws, client, lambda _h: 0.2, max_iters=5))
    assert summary["stop"]["state"] == "pending_recovery"
    assert client.submit_calls == 0
    assert client.wait_calls == (0 if unreachable else 1)
    events = read_log(summary["session_log"])
    pending = [event for event in events if event["kind"] == "recovery_pending"]
    assert pending and pending[-1]["payload"]["decline"] is False
    assert not any(event["kind"] == "propose" for event in events)


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


def test_campaign_config_defaults_sample_and_honors_override(tmp_path):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    base = {"workspace": ws, "client": MockTavi(lambda h: 0.0),
            "run_isar": lambda *a, **k: None, "calibration": _UNITY_CAL}
    cfg = _campaign_config(_objective(), dict(base))
    assert cfg.sample == "Al_phonon_DFT"
    cfg2 = _campaign_config(_objective(), dict(base, sample="Al_bragg_custom"))
    assert cfg2.sample == "Al_bragg_custom"
    # the resolved sample reaches the validate/scan body's parameters
    body = _validate_body(_scan_proposal(0.0, 1.0, 0.1), sample=cfg2.sample)
    assert body["parameters"]["sample"] == "Al_bragg_custom"


# --------------------------------------------------------------------------- proposal point-count guard
def _scan_proposal(e_lo, e_hi, e_step, kind="production"):
    nc = QUICKLOOK_NCOUNT if kind == "quicklook" else PRODUCTION_NCOUNT
    return ScanProposal(h=2.1, k=0.0, l=0.0, e_lo=e_lo, e_hi=e_hi, e_step=e_step, ncount=nc,
                        kind=kind, reason="test", n_points=points_in_window(e_lo, e_hi, e_step))


def test_emitted_point_count_is_unclamped():
    # The TRUE count TAVI computes from the command — NOT clamped to the acquisition budget (which
    # points_in_window is): a wide window with a tight step is exactly the over-limit case (the
    # documented 594-pt scan came from a widened window keeping a ~0.05 meV step).
    assert _emitted_point_count(-2.0, 27.65, 0.05) > DEFAULT_MAX_POINTS   # ~594, unclamped
    assert points_in_window(-2.0, 27.65, 0.05) == 100   # ledger clamp hides the true count


def test_guard_clamps_over_limit_proposal_keeping_window(tmp_path):
    # A 594-point proposal (the documented over-limit scan) is coarsened to fit the 200-point limit
    # WITHOUT moving the window bounds; a proposal_clamped event records the adjustment.
    log = tmp_path / "log.jsonl"
    p = _scan_proposal(-2.0, 27.65, 0.05)
    n_before = _emitted_point_count(p.e_lo, p.e_hi, p.e_step)
    assert n_before > DEFAULT_MAX_POINTS               # the documented over-limit (~594) scan
    clamped = _guard_point_count(p, DEFAULT_MAX_POINTS, "get_state", log, it=3)
    assert clamped is True
    # Window bounds are UNCHANGED (never truncated) — only the step coarsened.
    assert (p.e_lo, p.e_hi) == (-2.0, 27.65)
    assert p.e_step > 0.05
    # The emitted (TAVI-side) count now fits the limit.
    assert _emitted_point_count(p.e_lo, p.e_hi, p.e_step) <= DEFAULT_MAX_POINTS
    # A greppable proposal_clamped exception carries the before/after telemetry + limit provenance.
    ev = [e for e in read_log(log)
          if e["kind"] == "exception" and (e["payload"] or {}).get("action") == "proposal_clamped"]
    assert len(ev) == 1
    pl = ev[0]["payload"]
    assert pl["limit"] == 200 and pl["limit_source"] == "get_state"
    assert pl["old_n_points"] == n_before and pl["old_n_points"] > 200 and pl["new_n_points"] <= 200
    assert pl["new_step"] > pl["old_step"] and pl["hkl"] == [2.1, 0.0, 0.0] and pl["iter"] == 3


def test_guard_leaves_at_limit_proposal_untouched(tmp_path):
    # A proposal whose emitted count is exactly the limit is left as-is (no clamp, no event).
    log = tmp_path / "log.jsonl"
    p = _scan_proposal(0.0, 199.0, 1.0)                 # int(199/1)+1 == 200 == limit
    assert _emitted_point_count(p.e_lo, p.e_hi, p.e_step) == 200
    step_before = p.e_step
    assert _guard_point_count(p, 200, "get_state", log, it=0) is False
    assert p.e_step == step_before
    assert p.n_points == 200
    assert not read_log(log)                            # nothing logged for an in-limit proposal


def test_proposal_finalizer_uses_smaller_policy_cap_and_reclamps_neutron_count(tmp_path):
    p = ScanProposal(h=2.1, k=0.0, l=0.0, e_lo=0.0, e_hi=20.0, e_step=0.1,
                     ncount=200, kind="production", reason="test", n_points=100)
    objective = types.SimpleNamespace(steering={"max_scan_points": 10,
                                                "production_batch_ncount": 10,
                                                "dynamic_ncount": True})
    cfg = types.SimpleNamespace(max_points=50, max_points_source="get_state",
                                log_path=tmp_path / "log.jsonl")
    _finalize_proposal(
        p, objective, cfg, {"kind": "neutrons", "amount": 1_000.0, "spent": 0.0}, it=2)
    assert p.n_points == 10 and _emitted_point_count(p.e_lo, p.e_hi, p.e_step) == 10
    assert p.ncount == 100
    p.ncount = 5
    _finalize_proposal(
        p, objective, cfg, {"kind": "neutrons", "amount": 1_000.0, "spent": 0.0}, it=3)
    assert p.ncount == 10
    with pytest.raises(loop_mod.BudgetInsufficientForMinimumBatch,
                       match="budget_insufficient_for_minimum_batch"):
        _finalize_proposal(
            p, objective, cfg, {"kind": "neutrons", "amount": 99.0, "spent": 0.0}, it=4)


def test_proposal_fingerprint_persists_action_evidence_and_original_branch_roles(tmp_path):
    objective = types.SimpleNamespace(
        tolerance_meV=0.5,
        steering={"max_scan_points": 20, "production_batch_ncount": 10,
                  "max_ncount_per_scan": 1_000})
    cfg = types.SimpleNamespace(max_points=20, max_points_source="default",
                                instrument_limits=None, log_path=tmp_path / "log.jsonl")

    def finalized(target, rival):
        proposal = ScanProposal(
            h=2.1, k=0.0, l=0.0, e_lo=-2.0, e_hi=7.0, e_step=0.5,
            ncount=10, kind="production", reason="identity revisit", n_points=19,
            meta={"acquisition_action_role": "classification",
                  "authoritative_evidence_revision": "neighbor-master-r7",
                  "target_branch_role": target, "rival_branch_role": rival})
        _finalize_proposal(
            proposal, objective, cfg,
            {"kind": "neutrons", "amount": 10_000.0, "spent": 0.0}, it=1)
        return proposal

    acoustic = finalized("acoustic", "optic")
    optic = finalized("optic", "acoustic")
    assert acoustic.meta["acquisition_action_role"] == "classification"
    assert acoustic.meta["authoritative_evidence_revision"] == "neighbor-master-r7"
    assert len(acoustic.meta["proposal_fingerprint"]) == 64
    assert acoustic.meta["proposal_fingerprint"] != optic.meta["proposal_fingerprint"]

    restored = loop_mod._proposal_from_submission({
        "hkl": [2.1, 0.0, 0.0], "kind": "production", "submitted_ncount": 10,
        "acquisition_action_role": acoustic.meta["acquisition_action_role"],
        "authoritative_evidence_revision": acoustic.meta["authoritative_evidence_revision"],
        "target_branch_role": acoustic.meta["target_branch_role"],
        "rival_branch_role": acoustic.meta["rival_branch_role"],
        "proposal_fingerprint": acoustic.meta["proposal_fingerprint"],
    })
    assert restored.meta["target_branch_role"] == "acoustic"
    assert restored.meta["rival_branch_role"] == "optic"
    assert restored.meta["proposal_fingerprint"] == acoustic.meta["proposal_fingerprint"]


def test_finalizer_composes_per_point_and_queue_limits_for_hour_budget(tmp_path):
    p = ScanProposal(h=2.1, k=0.0, l=0.0, e_lo=0.0, e_hi=9.0, e_step=1.0,
                     ncount=400_000_000, kind="production", reason="test", n_points=10,
                     meta={"score": {"eig": 2.0, "cost": 4_000_000_000.0}})
    objective = types.SimpleNamespace(steering={"max_scan_points": 20,
                                                "production_batch_ncount": 100_000_000,
                                                "max_ncount_per_scan": 4_000_000_000})
    cfg = types.SimpleNamespace(
        max_points=20, max_points_source="get_state", log_path=tmp_path / "log.jsonl",
        instrument_limits={"max_points": 20, "max_neutrons_per_point": 150_000_000,
                           "queue_neutron_budget": 1_000_000_000, "max_queued": 4,
                           "sources": {"max_points": "get_state",
                                       "max_neutrons_per_point": "get_state",
                                       "queue_neutron_budget": "get_schema",
                                       "max_queued": "get_schema"}})
    _finalize_proposal(
        p, objective, cfg, {"kind": "counting_seconds", "amount": 60.0, "spent": 0.0}, it=7)
    assert p.ncount == 100_000_000
    assert p.meta["score"]["cost"] == 1_000_000_000.0
    clamp = [e["payload"] for e in read_log(cfg.log_path)
             if (e.get("payload") or {}).get("action") == "proposal_ncount_clamped_to_limit"]
    assert len(clamp) == 1
    assert clamp[0]["requested_ncount"] == 400_000_000
    assert clamp[0]["effective_ncount"] == 100_000_000
    assert clamp[0]["n_points"] == 10

    cfg.instrument_limits["queue_neutron_budget"] = 900_000_000
    with pytest.raises(loop_mod.InstrumentLimitBelowMinimumBatch,
                       match="instrument_limit_below_minimum_batch"):
        _finalize_proposal(
            p, objective, cfg, {"kind": "counting_seconds", "amount": 60.0, "spent": 0.0}, it=8)


def test_neutron_affordability_clamp_renormalizes_score(tmp_path):
    p = ScanProposal(
        h=2.1, k=0.0, l=0.0, e_lo=0.0, e_hi=9.0, e_step=1.0,
        ncount=400, kind="production", reason="test", n_points=10,
        meta={"score": {"method": "counts", "sigma_prior": 1.0, "c_spent": 1_000.0,
                         "classification_eig": 0.0, "classification_weight": 0.0,
                         "eig": 0.1, "combined_information": 0.1,
                         "cost": 4_000.0, "eig_per_cost": 0.1 / 4_000.0}})
    objective = types.SimpleNamespace(
        tolerance_meV=0.5,
        steering={"max_scan_points": 20, "production_batch_ncount": 10,
                  "max_ncount_per_scan": 1_000})
    cfg = types.SimpleNamespace(max_points=20, max_points_source="default",
                                instrument_limits=None, log_path=tmp_path / "log.jsonl")
    _finalize_proposal(
        p, objective, cfg, {"kind": "neutrons", "amount": 1_000.0, "spent": 0.0}, it=1)
    score = p.meta["score"]
    expected_eig = counts_eig(1.0, 0.5, 1_000.0, 1_000.0)
    assert p.ncount == 100
    assert score["cost"] == 1_000.0
    assert score["eig"] == pytest.approx(expected_eig)
    assert score["sigma_scan"] == pytest.approx(math.sqrt(0.5))
    assert score["eig_per_cost"] == pytest.approx(expected_eig / 1_000.0)


def test_preflight_reads_limit_from_state_limits_dict():
    class _C:
        def get_state(self):
            return {"mode": "allow", "busy": False, "limits": {"max_points": 128}}
    assert _preflight_max_points(_C()) == (128, "get_state")


def test_preflight_resolves_each_limit_from_state_then_schema():
    class _C:
        def get_state(self):
            return {"limits": {"max_points": 37, "max_neutrons_per_point": 100_000_000,
                               "queue_neutron_budget": "malformed"}}

        def get_schema(self):
            return {"limits": {"max_points": 99, "queue_neutron_budget": 1_000_000_000,
                               "max_queued": 4}}

    snapshot = _preflight_instrument_limits(_C())
    assert snapshot == {
        "max_points": 37, "max_neutrons_per_point": 100_000_000,
        "queue_neutron_budget": 1_000_000_000, "max_queued": 4,
        "sources": {"max_points": "get_state", "max_neutrons_per_point": "get_state",
                    "queue_neutron_budget": "get_schema", "max_queued": "get_schema"}}


def test_resume_restores_logical_proposal_counter_without_submissions(tmp_path):
    ws = tmp_path / "ws"
    log = ws / "report" / "session_log.jsonl"
    log_event(log, "propose", {"acquisition_id": "nonce:00000000"})
    log_event(log, "propose", {"acquisition_id": "nonce:00000001"})
    resumed = _resume_state(ws, log, _UNITY_CAL)
    assert resumed["proposal_counter"] == 2
    assert resumed["scan_counter"] == 0


def test_preflight_reads_top_level_limit_key():
    class _C:
        def get_state(self):
            return {"max_points": 150}
    assert _preflight_max_points(_C()) == (150, "get_state")


def test_preflight_falls_back_to_schema_then_default():
    # /state carries no limit but /schema does → schema is consulted.
    class _C:
        def get_state(self):
            return {"mode": "allow"}
        def get_schema(self):
            return {"limits": {"max_points": 64}}
    assert _preflight_max_points(_C()) == (64, "get_schema")


def test_preflight_falls_back_to_default_when_state_missing_or_raises():
    # Client without a state/schema method → default 200, source "default".
    assert _preflight_max_points(object()) == (DEFAULT_MAX_POINTS, "default")

    class _Boom:
        def get_state(self):
            raise RuntimeError("unreachable")
    assert _preflight_max_points(_Boom()) == (DEFAULT_MAX_POINTS, "default")


def test_extract_max_points_ignores_non_positive_and_bool():
    assert _extract_max_points({"max_points": 0}) is None
    assert _extract_max_points({"max_points": True}) is None      # bool is not a real limit
    assert _extract_max_points({"max_points": 200}) == 200
    assert _extract_max_points({"max_points": 0.5}) is None
    assert _extract_max_points({"max_points": 1.2}) is None
    assert _extract_max_points({"max_points": 1.0}) == 1
    assert _extract_max_points({"max_points": 1e8}) == 100_000_000
    assert _extract_max_points("nope") is None


class _StateTavi(MockTavi):
    """A MockTavi that also advertises a max-points limit via GET /state and records every submitted
    scan command, so a campaign test can assert nothing over-limit is ever submitted."""
    def __init__(self, omega, max_points):
        super().__init__(omega)
        self._max_points = max_points
        self.submitted_cmds = []

    def get_state(self):
        return {"mode": "allow", "busy": False, "limits": {"max_points": self._max_points}}

    def submit_scan(self, body, idempotency_key=None):
        self.submitted_cmds.append(body["parameters"]["scan_command1"])
        return super().submit_scan(body, idempotency_key=idempotency_key)


class _NcountLimitedTavi(MockTavi):
    def __init__(self, omega):
        super().__init__(omega)
        self.validated_ncounts = []
        self.submitted_ncounts = []

    def get_state(self):
        return {"limits": {"max_points": 200,
                           "max_neutrons_per_point": 100_000_000,
                           "queue_neutron_budget": 20_000_000_000,
                           "max_queued": 4}}

    def validate(self, body):
        self.validated_ncounts.append(body["parameters"]["number_neutrons"])
        return super().validate(body)

    def submit_scan(self, body, idempotency_key=None):
        self.submitted_ncounts.append(body["parameters"]["number_neutrons"])
        return super().submit_scan(body, idempotency_key=idempotency_key)


def test_campaign_clamps_dynamic_400m_batch_and_publishes_limit_envelope(
        tmp_path, monkeypatch):
    omega = lambda h: (h - 2.0) * 2.0
    client = _NcountLimitedTavi(omega)

    def desired_400m(*_args, **_kwargs):
        return ScanProposal(
            h=2.1, k=0.0, l=0.0, e_lo=-2.0, e_hi=7.0, e_step=0.1,
            ncount=400_000_000, kind="production", reason="dynamic tolerance request",
            e_pred=0.2, n_points=91,
            meta={"score": {"method": "deficit", "cost": 36_400_000_000.0},
                  "count_diagnostics": {"dynamic_ncount": True,
                                        "requested_before_runtime_limit": 400_000_000}})

    monkeypatch.setattr(loop_mod, "propose_scan", desired_400m)
    summary, _ws = _run(
        tmp_path, _objective(), client, make_mock_runner(omega), max_iters=1)
    assert client.validated_ncounts == [100_000_000]
    assert client.submitted_ncounts == [100_000_000]
    events = read_log(summary["session_log"])
    start = next(e["payload"] for e in events
                 if (e.get("payload") or {}).get("action") == "campaign_start")
    assert start["instrument_limits"]["discovered"]["max_neutrons_per_point"] == 100_000_000
    assert summary["instrument_limits"] == start["instrument_limits"]
    clamps = [e["payload"] for e in events
              if (e.get("payload") or {}).get("action") == "proposal_ncount_clamped_to_limit"]
    assert len(clamps) == 1
    assert clamps[0]["requested_ncount"] == 400_000_000
    assert clamps[0]["effective_ncount"] == 100_000_000


class _RejectingTavi(MockTavi):
    def validate(self, body):
        return {"would_queue": False, "blockers": ["unknown refusal"],
                "requested_points": _cmd_points(body["parameters"]["scan_command1"]),
                "feasible_points": _cmd_points(body["parameters"]["scan_command1"])}


def test_validation_refusals_use_distinct_logical_acquisitions(tmp_path):
    client = _RejectingTavi()
    summary, _ws = _run(
        tmp_path, _objective(), client, make_mock_runner(lambda h: (h - 2.0) * 2.0),
        max_iters=20)
    proposals = [e["payload"] for e in read_log(summary["session_log"])
                 if e["kind"] == "propose"]
    # Operational refusals are not scientific evidence that the q is infeasible. The global
    # iteration limit is the guardrail when the instrument never accepts a proposal.
    assert len(proposals) == 20
    assert len({proposal["acquisition_id"] for proposal in proposals}) == 20
    assert client.n == 0


def _cmd_points(cmd):
    _, lo, hi, step = cmd.split()
    return _emitted_point_count(float(lo), float(hi), float(step))


def test_campaign_never_submits_over_limit_scan(tmp_path):
    # ω beyond the survey ceiling forces missed-window recenters (which widen e_lo/e_hi without
    # recomputing the tight e_step) — the exact path that produced the 594-pt over-limit scan. With
    # the guard active every SUBMITTED command must fit the instrument's advertised limit, and the
    # clamp must have fired at least once (recenter windows exceed the small limit).
    omega = lambda h: 9.0
    client = _StateTavi(omega, max_points=50)
    summary, ws = _run(tmp_path, _objective(), client,
                       make_mock_runner(omega, window_gate=True), max_iters=40)
    assert client.submitted_cmds
    assert all(_cmd_points(c) <= 50 for c in client.submitted_cmds), \
        [(_cmd_points(c), c) for c in client.submitted_cmds if _cmd_points(c) > 50]
    clamps = [e for e in read_log(summary["session_log"])
              if e["kind"] == "exception" and (e["payload"] or {}).get("action") == "proposal_clamped"]
    assert clamps, "a widening recenter should have tripped the point-count guard"
    assert all(c["payload"]["limit"] == 50
               and c["payload"]["limit_source"] == "policy+get_state"
               for c in clamps)


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


def test_submission_failure_finishes_with_terminal_summary_instead_of_sticking(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    summary, _ws = _run(
        tmp_path, _objective(), _SubmitFailingTavi(omega), make_mock_runner(omega), max_iters=1)

    assert summary["stop"]["state"] == "incomplete"
    assert any("instrument submission failed" in reason
               for reason in summary["stop"]["reasons"])
    events = read_log(summary["session_log"])
    failures = [event for event in events if event["kind"] == "exception"
                and (event.get("payload") or {}).get("action") == "instrument_submission_failed"]
    assert len(failures) == 1 and failures[0]["payload"]["status"] == 503
    assert events[-1]["kind"] == "stop"


def test_post_submission_failure_remains_pending_for_resume_recovery(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    summary, _ws = _run(
        tmp_path, _objective(), _WaitFailingTavi(omega), make_mock_runner(omega), max_iters=1)

    assert summary["stop"]["state"] == "pending_recovery"
    assert any("press Resume" in reason for reason in summary["stop"]["reasons"])
    events = read_log(summary["session_log"])
    assert any(event["kind"] == "physical_submission" for event in events)
    pending = [event for event in events if event["kind"] == "recovery_pending"]
    assert len(pending) == 1 and pending[0]["payload"]["job_id"] == "j-1"
    assert not any((event.get("payload") or {}).get("action")
                   == "instrument_submission_failed" for event in events)
    assert events[-1]["kind"] == "stop"


# --------------------------------------------------------------------------- cooperative operator stop
class _StopFileTavi(MockTavi):
    """A MockTavi whose FIRST wait_scan writes the operator stop-file mid-wait — simulating an operator
    click during a long scan wait. The current iteration still completes; the loop exits at the next
    iteration boundary."""
    def __init__(self, omega, workspace, token):
        super().__init__(omega)
        self._control = Path(workspace) / "report" / "control"
        self._token = token
        self._armed = True

    def wait_scan(self, job_id, timeout=0.0, **kw):
        if self._armed:
            self._armed = False
            self._control.mkdir(parents=True, exist_ok=True)
            (self._control / f"stop-{self._token}.json").write_text(
                json.dumps({"requested_by": "gui", "reason": "operator click"}), encoding="utf-8")
        return super().wait_scan(job_id, timeout=timeout, **kw)


def _no_subprocess(monkeypatch):
    """Stub the end-of-campaign report/diagnostic subprocess renders (keep operator-stop tests fast)."""
    class _CP:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(loop_mod.subprocess, "run", lambda cmd, **k: _CP())


def test_operator_stop_completes_iteration_then_exits_cleanly(tmp_path, monkeypatch):
    # A stop-file written during the first wait_scan: the current iteration COMPLETES (scan persisted,
    # analysis ran), then the loop exits at the next boundary with an operator_stopped terminal state.
    _no_subprocess(monkeypatch)
    omega = lambda h: (h - 2.0) * 2.0
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    token = "tok-abc123"
    client = _StopFileTavi(omega, ws, token)
    cfg = {"workspace": ws, "client": client, "run_isar": make_mock_runner(omega),
           "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8}, "seed": 7,
           "control_token": token}
    summary = run_campaign(_objective(), cfg)

    assert summary["stop"]["state"] == "operator_stopped"
    assert "grid_state" in summary["stop"] and summary["stop"]["grid_state"]["state"] in (
        "continue", "met", "incomplete", "infeasible")
    reasons = summary["stop"]["reasons"]
    assert any("iteration" in r for r in reasons) and any(token in r for r in reasons)

    events = read_log(summary["session_log"])
    # The interrupted iteration ran to completion: data persisted + analysis happened.
    assert any(e["kind"] == "run" for e in events)
    assert any(e["kind"] == "analyze" for e in events)
    # Exactly one operator_stop acknowledgement decide.
    acks = [e for e in events if e["kind"] == "decide"
            and (e.get("payload") or {}).get("event") == "operator_stop"]
    assert len(acks) == 1 and acks[0]["payload"]["token"] == token
    assert acks[0]["payload"]["action"] == "operator_stop"     # action mirrors event
    # Certification was skipped entirely — a stop must never trigger the certify bootstrap.
    assert not any((e.get("payload") or {}).get("event") == "certify_start"
                   for e in events if e["kind"] == "decide")
    # Exactly ONE stop event and it is THE last event; it carries the cause + grid_state.
    stops = [e for e in events if e["kind"] == "stop"]
    assert len(stops) == 1 and events[-1]["kind"] == "stop"
    assert stops[0]["payload"]["state"] == "operator_stopped"
    assert "grid_state" in stops[0]["payload"]
    # Nothing is ever deleted: the control file is still there afterward.
    assert (ws / "report" / "control" / f"stop-{token}.json").exists()


def test_operator_stop_ignores_a_different_token(tmp_path):
    # A stale stop-file for a DIFFERENT token, present from the start, is inert — the campaign runs to
    # its natural (met) end.
    omega = lambda h: (h - 2.0) * 2.0
    ws = tmp_path / "ws"
    ctl = ws / "report" / "control"
    ctl.mkdir(parents=True)
    (ctl / "stop-SOMEONE_ELSE.json").write_text("{}", encoding="utf-8")
    cfg = {"workspace": ws, "client": MockTavi(omega), "run_isar": make_mock_runner(omega),
           "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8}, "seed": 7,
           "control_token": "MY_TOKEN"}
    summary = run_campaign(_objective(), cfg)
    assert summary["stop"]["state"] == "met", summary["stop"]["reasons"]
    events = read_log(summary["session_log"])
    assert not any((e.get("payload") or {}).get("event") == "operator_stop"
                   for e in events if e["kind"] == "decide")


def test_operator_stop_before_first_iteration_is_clean(tmp_path, monkeypatch):
    # Stop-file present before iteration 0 completes: clean operator_stopped, no crash on the
    # decision-None path (no iteration ever finished, so no scan was submitted).
    _no_subprocess(monkeypatch)
    omega = lambda h: (h - 2.0) * 2.0
    ws = tmp_path / "ws"
    ctl = ws / "report" / "control"
    ctl.mkdir(parents=True)
    token = "pre-start"
    (ctl / f"stop-{token}.json").write_text("{}", encoding="utf-8")
    cfg = {"workspace": ws, "client": MockTavi(omega), "run_isar": make_mock_runner(omega),
           "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8}, "seed": 7,
           "control_token": token}
    summary = run_campaign(_objective(), cfg)
    assert summary["stop"]["state"] == "operator_stopped"
    assert "grid_state" in summary["stop"]
    events = read_log(summary["session_log"])
    assert not any(e["kind"] == "run" for e in events)          # never submitted a scan
    acks = [e for e in events if e["kind"] == "decide"
            and (e.get("payload") or {}).get("event") == "operator_stop"]
    assert len(acks) == 1 and acks[0]["payload"]["iter"] == 0
    assert read_log(summary["session_log"])[-1]["kind"] == "stop"


def test_operator_stop_disabled_by_default_ignores_stop_file(tmp_path):
    # control_token unset (None default): a stop-file lying around is completely ignored — behaviour is
    # identical to today.
    omega = lambda h: (h - 2.0) * 2.0
    ws = tmp_path / "ws"
    ctl = ws / "report" / "control"
    ctl.mkdir(parents=True)
    (ctl / "stop-whatever.json").write_text("{}", encoding="utf-8")
    cfg = {"workspace": ws, "client": MockTavi(omega), "run_isar": make_mock_runner(omega),
           "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8}, "seed": 7}
    summary = run_campaign(_objective(), cfg)
    assert summary["stop"]["state"] == "met", summary["stop"]["reasons"]


def test_operator_stop_during_final_allowed_iteration_is_honored(tmp_path, monkeypatch):
    # The final-boundary case: with --max-iters 1 the stop file is written DURING the only allowed
    # iteration's scan, so no top-of-iteration check will ever see it. The boundary after the last
    # allowed iteration is still a boundary — the loop must honour the stop there (operator_stopped,
    # no certification) instead of falling through to normal termination.
    _no_subprocess(monkeypatch)
    omega = lambda h: (h - 2.0) * 2.0
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    token = "final-boundary"
    client = _StopFileTavi(omega, ws, token)
    cfg = {"workspace": ws, "client": client, "run_isar": make_mock_runner(omega),
           "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8}, "seed": 7,
           "control_token": token, "max_iters": 1}
    summary = run_campaign(_objective(), cfg)

    assert summary["stop"]["state"] == "operator_stopped"
    events = read_log(summary["session_log"])
    assert any(e["kind"] == "run" for e in events)              # the final iteration completed its scan
    acks = [e for e in events if e["kind"] == "decide"
            and (e.get("payload") or {}).get("event") == "operator_stop"]
    assert len(acks) == 1 and acks[0]["payload"]["iter"] == 1   # the post-exhaustion boundary (== max_iters)
    # Certification must not run on an operator stop, even on this path.
    assert not any((e.get("payload") or {}).get("event") == "certify_start"
                   for e in events if e["kind"] == "decide")
    stops = [e for e in events if e["kind"] == "stop"]
    assert len(stops) == 1 and events[-1]["kind"] == "stop"
    assert stops[0]["payload"]["state"] == "operator_stopped"


# --------------------------------------------------------------------------- per-invocation max_iters
def test_max_iters_restart_grants_fresh_cap(tmp_path):
    # --max-iters is per invocation: each run_campaign restarts iteration numbering at zero (range over
    # cfg.max_iters), so a second run with the same cap does MORE work rather than treating the cap as
    # already exhausted. (Not "one run event per invocation" — a single iteration may submit several
    # scans via recenter/widen retries.)
    omega = lambda h: (h - 2.0) * 2.0
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    base = {"workspace": ws, "run_isar": make_mock_runner(omega), "calibration": _UNITY_CAL,
            "resolution": {"fwhm_dE": 0.8}, "seed": 7, "max_iters": 1}
    s1 = run_campaign(_objective(), dict(base, client=MockTavi(omega)))
    runs1 = [e for e in read_log(s1["session_log"]) if e["kind"] == "run"]
    assert runs1                                               # the first capped invocation did work
    s2 = run_campaign(_objective(), dict(base, client=MockTavi(omega)))
    runs2 = [e for e in read_log(s2["session_log"]) if e["kind"] == "run"]
    assert len(runs2) > len(runs1)                            # the restart's fresh cap submitted more scans
    # The restart resumed the prior spend ledger (didn't reset) and started a fresh campaign.
    starts = [e for e in read_log(s2["session_log"])
              if (e.get("payload") or {}).get("action") == "campaign_start"]
    assert starts[-1]["payload"]["resumed"]["scan_counter"] == len(runs1)


# --------------------------------------------------------------------------- campaign_start fingerprint
def test_campaign_start_stamps_source_fingerprint_when_provided(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    src = {"kind": "goal", "sha256": "deadbeefcafe"}
    cfg = {"workspace": ws, "client": MockTavi(omega), "run_isar": make_mock_runner(omega),
           "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8}, "seed": 7, "source": src}
    summary = run_campaign(_objective(), cfg)
    starts = [e for e in read_log(summary["session_log"])
              if (e.get("payload") or {}).get("action") == "campaign_start"]
    assert starts and starts[-1]["payload"]["source"] == src


def test_campaign_start_omits_source_when_absent(tmp_path):
    # Backward compatible: no `source` config → the field is omitted from campaign_start.
    omega = lambda h: (h - 2.0) * 2.0
    summary, ws = _run(tmp_path, _objective(), MockTavi(omega), make_mock_runner(omega))
    starts = [e for e in read_log(summary["session_log"])
              if (e.get("payload") or {}).get("action") == "campaign_start"]
    assert starts and "source" not in starts[-1]["payload"]


# --------------------------------------------------------------------------- force-kill characterization
def test_force_kill_run_without_results_row_counts_spend(tmp_path):
    # Documents the KNOWN cost of a hard kill (the reason the cooperative stop exists): a `run` event
    # whose scan never produced a results.csv row (killed mid-analysis) still counts toward the resumed
    # spend ledger, but leaves no folded observation — so the next invocation re-proposes for that q and
    # double-spends. The cooperative operator stop avoids this by finishing the iteration first.
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    log = ws / "report" / "session_log.jsonl"
    log_event(log, "run", {"job_id": "j1", "state": "done", "eta": {"estimated_seconds": 5.0},
                           "kind": "production", "ncount": PRODUCTION_NCOUNT, "n_points": 60,
                           "hkl": [2.1, 0.0, 0.0], "cost": 6.0e9})
    # No results.csv written at all — the analysis never landed a row for that scan.
    r = _resume_state(ws, log, _UNITY_CAL)
    assert r["scan_counter"] == 1                               # the submitted scan is on the ledger
    assert r["neutrons"] == pytest.approx(6.0e9)                # its spend is counted (no free re-do)
    assert r["observations"] == []                             # but nothing folded → the q looks unmeasured
