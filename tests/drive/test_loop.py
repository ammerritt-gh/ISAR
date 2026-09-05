"""run_campaign orchestration against a MOCK TAVI client + a MOCK isar runner (no network, no fits).

The mock isar runner is the analysis boundary stand-in: it reads the saved scan JSON and writes a
``report/results.csv`` exactly as ISAR would, so the loop exercises the real readers, real σ-calibration,
real acquisition, and real stopping — only the fitting and the instrument are faked.
"""
import collections
import csv
import json
import math
import shutil
from pathlib import Path

import pytest

from isar.drive.acquisition.calibration import Calibration
import types

import isar.drive.loop as loop_mod
from isar.drive.loop import (_campaign_config,
                             _campaign_summary,
                             _declined_observation,
                             _errors_arg_for, _errors_tier_for, _errors_tiers_for,
                             _full_tier,
                             _finalize_proposal, _guard_point_count,
                             _record_eligible_crossings,
                             resolve_campaign_slice,
                             _pending_certification_indices, _record_certification_acceptance,
                             _pending_anchor_escalations, _unfolded_master,
                             _planner_candidates, _candidate_probabilities, _slice_rows,
                             _master_obs_from_row, _observation_from_row, _pick_interval,
                             _proposal_from_submission,
                             _reconcile_budget_points, _resume_state,
                             _run_nonce,
                             run_campaign)
from isar.drive.instrument import request_realization as wire
from isar.drive.instrument.request_realization import (DEFAULT_MAX_POINTS, _emitted_point_count,
                                                       _executed_point_count,
                                                       _extract_max_points, _feasible_point_count,
                                                       _preflight_instrument_limits,
                                                       _preflight_max_points,
                                                       _target_supporting_feasible_points,
                                                       _validate_body)
from isar.drive.acquisition.acquire import ScanProposal
from isar.drive.acquisition.scan_grid import MAX_POINTS
from isar.drive.acquisition.eig_planner import default_intensity_scale
from isar.drive.acquisition.grid_adapters import TAVI_GRID_ADAPTER
from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot
from isar.drive.supervisor import goal_to_spec as g2s
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

_UNITY_CAL = Calibration.from_dict({"inflation": 1.0, "buckets": [], "center_pull_mean": 0.0})


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


# -- inventory persistence (PR1.6) -----------------------------------------------------------------

def test_a_campaign_records_all_three_action_identities_against_each_scan(tmp_path):
    """PR2.2: a scan must be joinable back to the scientific question it answers, the exact request
    that was sent, and the geometry TAVI judged that request under. With only the request recorded, a
    forensic replay cannot tell a geometry rejection (retry elsewhere) from a scientifically exhausted
    action (never retry) — which is exactly the distinction PR2.3's reject-and-try-next rests on."""
    omega = lambda h: (h - 2.0) * 2.0
    summary, _ws = _run(tmp_path, _objective(), MockTavi(omega), make_mock_runner(omega))
    events = read_log(summary["session_log"])

    validations = [e["payload"] for e in events if e["kind"] == "validate"]
    assert validations, "a campaign that submitted scans must have validated them"
    assert all(v.get("feasibility_manifest_fingerprint") for v in validations)

    runs = [e["payload"] for e in events if e["kind"] == "run"]
    assert runs
    for payload in runs:
        assert payload.get("proposal_fingerprint")
        assert payload.get("scientific_action_fingerprint")
        assert payload.get("feasibility_manifest_fingerprint")


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
                "members": members, "track_state": "confirmed", "track_revision": 1}

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
    """The exact point list TAVI executes for a ``scan_command1``.

    Goes through the production grid adapter rather than reimplementing the arithmetic: TAVI counts
    ``floor((hi - lo) / step + 0.5) + 1`` on the ``%g``-rendered wire values, and a truncating
    ``int((hi - lo) / step) + 1`` here disagrees by a point on windows whose span is a hair under a
    whole number of steps — which would silently mis-align every feasibility mask a mock builds.
    """
    _, lo, hi, step = scan_command.split()
    lo, hi, step = float(lo), float(hi), float(step)
    return list(TAVI_GRID_ADAPTER.executed_energies(
        lo, step, TAVI_GRID_ADAPTER.point_count(lo, hi, step)))


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

    def get_schema(self):
        source_ids = (
            "environment_flat", "environment_slope", "environment_cosmic_spikes",
            "instrument_aluminum_powder", "sample_elastic", "sample_elastic_tail",
        )
        return {"background": {
            "background_schema": "tavi.background/2",
            "catalog_version": 2,
            "sources": {source_id: {} for source_id in source_ids},
        }}


class _TargetSupportTavi(MockTavi):
    def validate(self, body):
        grid = _parse_grid(body["parameters"]["scan_command1"])
        return {
            "would_queue": True,
            "blockers": [],
            "eta": {"estimated_seconds": 5.0},
            "requested_points": len(grid),
            "feasible_points": len(grid),
            "point_manifest": [
                {"feasible": True, "values": {"deltaE": energy}}
                for energy in grid
            ],
        }


class _UnreachableQTavi(MockTavi):
    """A TAVI where one q is geometrically unreachable — every request there is refused."""

    def __init__(self, omega_true, *, unreachable_h):
        super().__init__(omega_true)
        self.unreachable_h = unreachable_h
        self.validated_h: list[float] = []

    def validate(self, body):
        h = float(body["parameters"]["H"])
        self.validated_h.append(h)
        if abs(h - self.unreachable_h) <= 1e-6:
            return {"would_queue": False, "blockers": ["a2_out_of_range"],
                    "requested_points": 91, "feasible_points": 0}
        return super().validate(body)


def test_an_unreachable_q_is_recorded_as_a_geometry_suppression(tmp_path):
    """PR2.3. Before this, a q the instrument cannot reach cost a whole iteration every time the
    selector picked it, and nothing recorded that it had stopped being usable — so an inventory could
    empty out with no account of why."""
    omega = lambda h: (h - 2.0) * 2.0
    client = _UnreachableQTavi(omega, unreachable_h=2.1)
    summary, _ws = _run(tmp_path, _objective(), client, make_mock_runner(omega))
    events = read_log(summary["session_log"])

    suppressed = [e["payload"] for e in events if e["kind"] == "action_suppressed"]
    assert suppressed, "an unreachable q must be recorded, not silently retried forever"
    assert suppressed[0]["scope"] == "geometry"
    assert suppressed[0]["rejection_class"] == "structural"
    assert suppressed[0]["verdict"] == "geometry_unreachable"
    assert suppressed[0]["hkl"][0] == pytest.approx(2.1)

    # No scan was ever submitted at the unreachable q, and the campaign kept working elsewhere.
    submitted = [float(body["parameters"]["H"]) for body in client.jobs.values()]
    assert all(abs(h - 2.1) > 1e-6 for h in submitted)
    assert summary["stop"]["state"] in {"met", "incomplete"}


def test_a_suppressed_q_is_not_re_proposed_on_later_iterations(tmp_path):
    """The suppression has to outlive its iteration. Held only within one, the selector would pick the
    same unreachable q again next time and the campaign would burn its remaining iterations on it."""
    omega = lambda h: (h - 2.0) * 2.0
    client = _UnreachableQTavi(omega, unreachable_h=2.1)
    summary, _ws = _run(tmp_path, _objective(), client, make_mock_runner(omega))
    rejected_validations = [h for h in client.validated_h if abs(h - 2.1) <= 1e-6]
    assert len(rejected_validations) == 1, \
        f"the unreachable q was validated {len(rejected_validations)} times; suppression did not stick"


class _SubmitFailingTavi(MockTavi):
    def submit_scan(self, body, idempotency_key=None):
        raise TaviError(503, "POST /scan -> HTTP 503: instrument unavailable")


class _WaitFailingTavi(MockTavi):
    def wait_scan(self, job_id, timeout=0.0, **kw):
        raise TaviError(None, f"GET /scan/{job_id} -> transport error: connection lost")


import math as _math


def _write_belief_models(workspace, anchors, branch, *, sample="Al_phonon_DFT", t_bin=200.0):
    """Persist the two branch beliefs the drive reads, as ``isar run`` really does.

    The EIG planner's hypothesis map IS ``card.state.json["branch_belief_models"]``: without it the
    beliefs never leave their class prior, no q ever gets its target energy pinned, and a campaign
    against these fakes can never reach the prize. ``anchors`` is ``{u: omega0}`` for the branch the
    fake delivered; the rival is carried prior-only, exactly as ``_store_branch_beliefs`` leaves it
    before its first fit lands.
    """
    from isar.schema.hypotheses import preset
    rows = []
    for name in ("acoustic", "optic"):
        hypothesis = preset(name, (-2.0, 12.0)).to_dict()
        rows.append({
            "sample": sample, "t_bin": t_bin, "branch_id": name, "period": 2.0,
            "anchors": ([{"u": float(u), "omega0": float(omega), "weight": 1.0}
                         for u, omega in sorted(anchors.items())] if name == branch else []),
            "n_anchors": (len(anchors) if name == branch else 0),
            "parameter_prior": {"zc_energy": hypothesis["zc_energy"],
                                "slope_zc": hypothesis["slope_zc"],
                                "confidence": hypothesis["confidence"]},
            "label_probabilities": {}})
    path = Path(workspace) / "card.state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema_version": 1}
    state["branch_belief_models"] = rows
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _reduced(h, period=2.0):
    """The reduced-zone coordinate the belief anchors are stored in (``branch_assignment.reduced_q``)."""
    half = 0.5 * period
    return abs(((float(h) + half) % period) - half)


def make_mock_runner(omega_true, *, window_gate=True, hesse_only=False, blank_members=False,
                     branch="acoustic", model_robust=True):
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
                             "confidence": 0.9, "reasons": "resolved",
                             # Every fitted outcome publishes its modes, exactly as
                             # ``_materialize_branch_candidates`` does in the real pipeline. The EIG
                             # planner reads them to know WHICH branch a scan delivered — and so
                             # which branch its window is evidence AGAINST.
                             "branch_id": branch,
                             "branch_candidates": json.dumps([
                                 {"candidate_ref": f"{scan_id}:det:mode:0", "energy_meV": true,
                                  "sigma_meV": raw_hw, "omega0_err": raw_hw, "amplitude": 12.0,
                                  "probabilities": {branch: 0.97, "new": 0.03}}])})
                if hesse_only and n > QUICKLOOK_NCOUNT:
                    base.update({"hesse_lo": true - raw_hw, "hesse_hi": true + raw_hw,
                                 "hesse_trusted": True})
                else:
                    base.update({"interval_lo": true - raw_hw, "interval_hi": true + raw_hw})
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
                    "model_interval_hi": true + hw, "model_robust": model_robust,
                    # Chain of custody: member labels (";"-joined). blank_members leaves the cell
                    # empty while n_members>0 to exercise the summary's evidence-chain guard.
                    "members": ("" if blank_members else ";".join(str(i) for i in range(npool))),
                    "confidence": 0.9, "reasons": "pooled master",
                    "branch_id": branch,
                    "branch_candidates": json.dumps([
                        {"candidate_ref": f"{master_scan}:det:mode:0", "energy_meV": true,
                         "sigma_meV": hw, "omega0_err": hw, "amplitude": 12.0,
                         "probabilities": {branch: 0.97, "new": 0.03}}]),
                    "selected_candidate_index": 0,
                    "amplitudes": json.dumps({"phonon.A": 12.0, "elastic.E": 30.0, "bg.BG": 0.3}),
                    "effective_exposure": float(QUICKLOOK_NCOUNT),
                    # A real campaign maps a declared branch, so its pooled master publishes
                    # where the point sits on that track; without it the point never converges.
                    "track_state": "confirmed", "track_revision": 1}
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
                "interval_reliable", "hesse_lo", "hesse_hi", "hesse_trusted", "branch_id",
                "pooled", "n_members", "members", "model_interval_lo", "model_interval_med",
                "model_interval_hi", "model_robust", "reasons", "branch_candidates",
                "selected_candidate_index", "amplitudes", "effective_exposure",
                "track_state", "track_revision"]
        with open(report / "results.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c) for c in cols})
        _write_belief_models(
            workspace, {_reduced(h): omega_true(h) for h in prod_per_h}, branch)
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


def test_master_certifies_a_selected_secondary_mode_on_its_own_interval():
    """PR #37 review P1, measured at h=3.0 on the reference path: the model-averaged interval is
    the fit's PRIMARY mode (the optic, 7.95-8.00) while the tracker selected the acoustic at 5.99 as
    the target. That interval cannot certify this row; the selected mode certifies on its own
    bootstrap interval, and its omega0 is the selected energy, not the primary mode's."""
    m = _master_obs_from_row(
        _urow(omega0=5.99, interval_lo=5.98, interval_hi=6.00, interval_reliable=True,
              model_interval_lo=7.95, model_interval_med=7.97, model_interval_hi=8.00,
              model_robust=True),
        _UNITY_CAL)
    assert m["model_robust"] is False
    assert m["certifiable"] is True and m["omega0"] == pytest.approx(5.99)
    assert m["halfwidth"] == pytest.approx(0.01)
    # with no interval of its own, the selected mode is not certifiable -- and never inherits
    m = _master_obs_from_row(
        _urow(omega0=5.99, interval_lo=None, interval_hi=None, interval_reliable=None,
              hesse_lo=None, hesse_hi=None,
              model_interval_lo=7.95, model_interval_med=7.97, model_interval_hi=8.00,
              model_robust=True),
        _UNITY_CAL)
    assert m["certifiable"] is False and m["halfwidth"] is None
    # the model interval still certifies the mode it describes
    m = _master_obs_from_row(
        _urow(omega0=7.96, model_interval_lo=7.95, model_interval_med=7.97,
              model_interval_hi=8.00, model_robust=True),
        _UNITY_CAL)
    assert m["model_robust"] is True and m["omega0"] == pytest.approx(7.97)


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


# The driver has no iteration cap — the budget is the only stop condition — so a test campaign is
# bounded by its neutron budget. ~2e11 funds a few tens of scans against the mock instrument: enough
# for every behaviour these tests exercise, and short enough that a campaign which never converges
# (a deliberately unreachable tolerance, a repeatedly refused window) still ends in seconds.
def _objective(tol=0.5, amount=2e11):
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=tol,
                                 budget={"kind": "neutrons", "amount": amount}, name="mvp")


_SCAN_BOUND_TOKEN = "scan-bound"


def _stop_after_scans(client, workspace, count):
    """Arm a cooperative operator stop once ``count`` requests have been validated.

    The loop has no iteration cap, so a test that only needs the campaign's first request bounds it
    the way an operator does — a stop file honoured at the next iteration boundary. The in-flight
    iteration still completes (submit -> wait -> persist -> analyze), so the first submission is
    fully recorded. Armed on ``validate`` rather than ``wait_scan`` because every realized request
    is validated, including one the instrument then refuses.
    """
    control = Path(workspace) / "report" / "control"
    original = client.validate
    seen = {"n": 0}

    def validate(body):
        seen["n"] += 1
        if seen["n"] >= count:
            control.mkdir(parents=True, exist_ok=True)
            (control / f"stop-{_SCAN_BOUND_TOKEN}.json").write_text(
                json.dumps({"requested_by": "test", "reason": "scan bound"}), encoding="utf-8")
        return original(body)

    client.validate = validate
    return client


def _run(tmp_path, objective, client, runner, *, stop_after=None, **extra):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True, exist_ok=True)
    if stop_after is not None:
        _stop_after_scans(client, ws, stop_after)
        extra.setdefault("control_token", _SCAN_BOUND_TOKEN)
    cfg = {"workspace": ws, "client": client, "run_isar": runner, "calibration": _UNITY_CAL,
           "resolution": {"fwhm_dE": 0.8}, "seed": 7, **extra}
    return run_campaign(objective, cfg), ws


def test_certify_failure_is_classified_as_controller_fault(tmp_path, monkeypatch):
    # Change 2 (AP7): the campaign converges (met) in-loop, then the forced final-certification pass
    # does not complete. A certify failure reached from a `met` decision is a controller fault (e.g.
    # the worklist-index invariant violation that hard-failed AP7), never a data-limited scientific
    # outcome. It must terminate `incomplete` with terminal_cause="controller_fault" so a software
    # bug is surfaced for repair-and-resume rather than reported as a certified/data-limited campaign.
    omega = lambda h: (h - 2.0) * 2.0
    monkeypatch.setattr(loop_mod, "_certify", lambda **kw: (kw["observations"], False))

    summary, _ws = _run(
        tmp_path, _objective(), MockTavi(omega), make_mock_runner(omega, hesse_only=True))

    assert summary["stop"]["state"] == "incomplete"
    assert summary["stop"].get("terminal_cause") == "controller_fault"
    assert "controller_fault" in summary["stop"]["reasons"][0]


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


# --------------------------------------------------------------------------------- SCENE-4 PR 4.4 D2
# SCENE-4 PR 4.4 D1 flipped the compile-time scan_window_eig_mode default from "off" to
# "authoritative". Default-on realization is SHARED behaviour between the discovery and legacy
# targeted (tracker) paths (4.3 already isolated discovery on AgendaVerdict): flipping the compile
# default must move ONLY the scan-window realization decisions on the targeted path -- zero change
# to target selection, tracker authority, or the stop decision. This is the AP7/AP8-class replay the
# PR 4.4 slice calls for, built on the smallest existing tracker fixture that reaches a real "met"
# stop under the mock instrument (test_campaign_reaches_met, above) rather than a new world.
_D2_FLIP_GOAL = {"path": {"h_start": 2.1, "h_end": 2.4, "k": 0.0, "l": 0.0},
                 "step_h": 0.1, "tolerance_meV": 0.5, "budget_hours": 8.0,
                 # goal_to_spec defaults an unset selector to "tracked_branch" (branch-tracking
                 # evidence the plain mock runner below does not emit); pin the per-q selector this
                 # fixture actually exercises so the flip is isolated to scan-window realization.
                 "target_selector": "lowest_energy",
                 "notes": "SCENE-4 PR 4.4 D2: targeted-path scan-window-eig-mode flip regression"}

# Session-log event kinds that scan-window realization owns -- expected to diverge between "off"
# and "authoritative" by construction (different window bounds/candidates, different provenance
# fingerprints/refs), and therefore excluded from the "everything else is identical" comparison.
_SCAN_WINDOW_EVENT_KINDS = {
    "scan_design_realization", "scan_design_realized_comparison", "scan_design_authority_decision",
    "eig_decision",
    # The action-inventory generator registry adds alternative-window candidate generators (e.g.
    # ``narrow_rival_probe``) once scan-window EIG is on ("shadow" or "authoritative") -- inventory
    # BUILDING and its analysis-clock timing markers are part of the same scan-window apparatus,
    # even though none of those candidates are ever selected (verified below: the propose/decide/stop
    # sequence, which records what was actually chosen, is unaffected).
    "analysis_clock", "inventory",
}


def _d2_branch_aware_runner(omega_true, target_hypothesis):
    """``make_mock_runner``, plus the ``branch_id`` column a hypothesis-carrying objective needs to
    resolve target identity. A compiled objective always freezes a rival hypothesis alongside the
    target (:func:`shipped_hypothesis_snapshot`), but with no distractor peak in this fixture's
    spectrum (a single ``omega_true`` line) every resolved row is unambiguously the target branch."""
    assigned: dict[str, int] = {}

    def runner(workspace, data_paths, **_kw):
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
            true = omega_true(h)
            contained = grid[0] <= true <= grid[-1]
            base = {"scan": scan_id, "detector": "det", "h": h, "k": k, "l": l, "status": "fitted",
                    "interval_reliable": True, "branch_id": target_hypothesis}
            if not contained:
                base.update({"verdict": "no_signal", "disposition": "declined", "omega0": None,
                             "interval_lo": None, "interval_hi": None, "reasons": "peak outside window"})
            else:
                raw_hw = 0.6 if n <= QUICKLOOK_NCOUNT else 0.3
                base.update({"verdict": "resolved", "disposition": "resolved", "omega0": true,
                             "interval_lo": true - raw_hw, "interval_hi": true + raw_hw,
                             "confidence": 0.9, "reasons": "resolved",
                             "branch_candidates": json.dumps([
                                 {"candidate_ref": f"{scan_id}:det:mode:0", "energy_meV": true,
                                  "sigma_meV": raw_hw, "omega0_err": raw_hw, "amplitude": 12.0,
                                  "probabilities": {target_hypothesis: 0.97, "new": 0.03}}])})
                prod_per_h[h] = prod_per_h.get(h, 0) + 1
            rows.append(base)
        master_scan = 9_000_000
        for h, npool in sorted(prod_per_h.items()):
            hw = 0.3 / _math.sqrt(npool)
            true = omega_true(h)
            rows.append({
                "scan": master_scan, "detector": "det", "h": h, "k": 0.0, "l": 0.0,
                "status": "fitted", "verdict": "resolved", "disposition": "resolved",
                "omega0": true, "interval_reliable": True, "pooled": True, "n_members": npool,
                "branch_id": target_hypothesis,
                "interval_lo": true - hw, "interval_hi": true + hw,
                "model_interval_lo": true - hw, "model_interval_med": true,
                "model_interval_hi": true + hw, "model_robust": True,
                "members": ";".join(str(i) for i in range(npool)),
                "branch_candidates": json.dumps([
                    {"candidate_ref": f"{master_scan}:det:mode:0", "energy_meV": true,
                     "sigma_meV": hw, "omega0_err": hw, "amplitude": 12.0,
                     "probabilities": {target_hypothesis: 0.97, "new": 0.03}}]),
                "selected_candidate_index": 0,
                "amplitudes": json.dumps({"phonon.A": 12.0, "elastic.E": 30.0, "bg.BG": 0.3}),
                "effective_exposure": float(QUICKLOOK_NCOUNT),
                "confidence": 0.9, "reasons": "pooled master",
                "track_state": "confirmed", "track_revision": 1})
            master_scan += 1
        report = Path(workspace) / "report"
        report.mkdir(parents=True, exist_ok=True)
        cols = ["scan", "detector", "h", "k", "l", "status", "verdict", "disposition",
                "confidence", "omega0", "omega0_err", "interval_lo", "interval_hi",
                "interval_reliable", "branch_id",
                "pooled", "n_members", "members", "model_interval_lo", "model_interval_med",
                "model_interval_hi", "model_robust", "reasons", "branch_candidates",
                "selected_candidate_index", "amplitudes", "effective_exposure",
                "track_state", "track_revision"]
        with open(report / "results.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c) for c in cols})
        _write_belief_models(
            workspace, {_reduced(h): omega_true(h) for h in prod_per_h}, target_hypothesis)
    return runner


def _d2_flip_campaign(tmp_path, mode):
    payload = _D2_FLIP_GOAL if mode == "authoritative" else {
        **_D2_FLIP_GOAL, "steering": {"scan_window_eig_mode": mode}}
    objective = g2s.spec_from_dict(payload)
    assert objective.steering["scan_window_eig_mode"] == mode
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    runner = _d2_branch_aware_runner(omega, objective.target_hypothesis)
    root = tmp_path / mode
    # The O41 shadow/realized-comparison machinery re-opens the workspace to read card policy; a
    # bare report/ dir (the plain ``_run`` shape) fails that open and falls back to legacy control
    # via a logged exception -- a real "off" vs "authoritative" fixture asymmetry, not a scan-window
    # decision. Give it a real card so the authoritative arm exercises the actual O41 path cleanly.
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        Path(__file__).resolve().parents[2] / "isar" / "parsers" / "cards" / "tavi.policy.toml",
        ws / "card.policy.toml")
    summary, ws = _run(root, objective, client, runner)
    return summary, ws


_WINDOW_OWNED_KEYS = {
    "e_lo", "e_hi", "request_fingerprint", "candidate_id", "candidate_role",
    "authority_refresh", "scan_window_eig", "scan_window_eig_mode",
    # ``meta.raw`` on the embedded objective is this test's own goal-dict echo -- the ONE place the
    # steering override that sets up the "off" arm is deliberately asymmetric with the "authoritative"
    # arm's absent override (both compile to the same effective mode). It carries no tracker
    # decision, only provenance of how the test built the fixture.
    "meta",
    # A random uuid4 minted once per WORKSPACE (``_run_nonce``), not derived from the objective or
    # its scan_window_eig_mode at all -- the two arms are two separate tmp_path workspaces, so their
    # nonces (and every acquisition_id built from one) differ regardless of mode.
    "acquisition_id", "run_nonce",
    # Steering-derived identity: recomputed from the steering dict, so it moves whenever the mode
    # does -- window-owned by construction, never a tracker decision.
    "hypothesis_fingerprint",
    # The O41 authority/comparison bookkeeping the authoritative arm mints and the "off" arm never
    # touches -- the exact records assertion (b) checks EXIST separately, so stripping them here
    # cannot make that check vacuous.
    "decision_fingerprint", "comparison_fingerprint", "comparison_ref",
    "scan_design_realized_comparison_ref", "scan_design_realized_comparison_fingerprint",
}


def _strip_window_owned(value):
    """Recursively drop the ENUMERATED scan-window-owned fields -- nothing pattern-matched.

    ``campaign_start`` embeds the WHOLE compiled objective (including its own
    ``steering.scan_window_eig_mode`` and ``scan_window_eig`` policy) -- exactly the field under
    test, so it must be stripped at any nesting depth, not only at the payload's top level.
    Every stripped key is listed by name in ``_WINDOW_OWNED_KEYS``: a broad predicate (any
    ``*fingerprint*`` / ``*_ref`` / ``scan_design_*`` key) could silently strip a genuine
    target/tracker divergence and make the sequence comparison vacuous.
    """
    if isinstance(value, dict):
        return {k: _strip_window_owned(v) for k, v in value.items()
                if k not in _WINDOW_OWNED_KEYS}
    if isinstance(value, list):
        return [_strip_window_owned(v) for v in value]
    return value


# The decision-bearing event kinds the brief actually asks about: which target/point was chosen
# (propose), promotion/certification/analysis decisions (decide, certification), and the final stop
# decision (stop). Deliberately NOT "validate"/"run"/"physical_submission"/"timing"/"analyze": those
# are TAVI-wire and instrumentation detail that legitimately doubles up under "authoritative" (an
# extra target_core validate round-trip per iteration, wall-clock jitter) without moving any
# target/tracker/stop decision -- the realized WINDOW those calls carry is already pinned via
# ``summary["grid"]``/``summary["dispersion"]`` (fitted omega0/convergence) matching exactly above.
# ``differentiation_probe`` is included because a probe decision is target selection (which rival
# the tracker chose to discriminate); this fixture may emit none, and an empty-vs-nonempty
# difference between the arms would itself be a caught divergence.
_D2_DECISION_EVENT_KINDS = {"propose", "decide", "stop", "certification",
                            "action_suppressed", "differentiation_probe"}


def _d2_tracker_signature(events):
    """The target/tracker/stop decision sequence, with scan-window-owned fields stripped out.

    ``propose``/``decide`` carry the realized window's own bounds and fingerprints (``e_lo``/
    ``e_hi``/``*_fingerprint``/``*_ref``) -- exactly what is allowed to move under the flip.
    Everything else about *which* point, *which* branch, and *what* decision was reached must not.
    """
    return [(event["kind"], _strip_window_owned(event.get("payload") or {}))
            for event in events if event["kind"] in _D2_DECISION_EVENT_KINDS]


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


def test_a_per_q_energy_selector_is_refused_because_the_planner_maps_a_branch():
    """The planner's prize is the DECLARED branch, so a per-q nearest/lowest/highest selector has a
    prize it can never satisfy and the campaign would run until its budget died. Refuse it at
    construction instead, naming the branch route."""
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    with pytest.raises(ValueError, match="target_branch"):
        ObjectiveSpec.segment(
            q_start=(2.0, 0.0, 0.0), q_end=(2.0, 0.2, 0.0), step_rlu=0.1,
            tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1.0e12},
            name="optic-nearest", target_branch="optic", target_label="transverse optic",
            target_selector="nearest_energy", energy_search_meV=(-2.0, 12.0), **frozen)


def test_session_log_shows_propose_analyze_decide_stop_chain(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client, make_mock_runner(omega))
    kinds = [e["kind"] for e in read_log(summary["session_log"])]
    for expected in ("propose", "validate", "run", "analyze", "decide", "stop"):
        assert expected in kinds, f"missing {expected} in session log ({set(kinds)})"
    assert kinds[-1] == "stop"


def test_campaign_incomplete_on_budget(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(amount=1.0), client, make_mock_runner(omega))
    assert summary["stop"]["state"] == "incomplete"
    assert any("budget" in r for r in summary["stop"]["reasons"])


_INFEASIBLE_POINTS_BODY = json.dumps({"error": {
    "code": "infeasible_points",
    "message": "One or more requested scan points are infeasible",
}})


class _PartialFeasibilityTavi(MockTavi):
    """A TAVI with a hard low-energy cutoff: below it the scattering triangle does not close.

    The cutoff is pinned, on the first request seen, just above that request's lowest energy — the
    real 2026-08-04 geometry, where exactly the ΔE = −10 meV edge point at Q = 2.7 was unreachable.
    It is an *energy* threshold, not an index, so a trimmed grid is genuinely feasible.

    Validation reports the partial manifest TAVI really sends, and ``submit_scan`` refuses any grid
    that still contains an infeasible point with the same HTTP 400 the instrument returns — the
    failure that cost that campaign every one of its scans.
    """

    def __init__(self, omega_true):
        super().__init__(omega_true)
        self.min_feasible_energy: float | None = None
        self.validated_grids: list[list[float]] = []

    def _mask(self, grid):
        return [energy >= self.min_feasible_energy for energy in grid]

    def validate(self, body):
        grid = _parse_grid(body["parameters"]["scan_command1"])
        if self.min_feasible_energy is None:
            self.min_feasible_energy = grid[0] + 0.5 * (grid[1] - grid[0])
        self.validated_grids.append(grid)
        mask = self._mask(grid)
        return {"would_queue": True, "blockers": [], "eta": {"estimated_seconds": 5.0},
                "requested_points": len(grid), "feasible_points": sum(mask),
                "planned_feasible_mask": mask, "partial": not all(mask),
                "point_manifest": [{"feasible": feasible, "values": {"deltaE": energy}}
                                   for feasible, energy in zip(mask, grid)]}

    def submit_scan(self, body, idempotency_key=None):
        grid = _parse_grid(body["parameters"]["scan_command1"])
        if not all(self._mask(grid)):
            raise TaviError(400, "POST /scan -> HTTP 400", _INFEASIBLE_POINTS_BODY)
        return super().submit_scan(body, idempotency_key=idempotency_key)


class _FirstPointInfeasibleTavi(MockTavi):
    """Pathological instrument: whatever grid it is handed, its FIRST point is infeasible.

    Trimming can never converge here, which is exactly the case the one-trim-per-proposal bound
    exists for.
    """

    def validate(self, body):
        grid = _parse_grid(body["parameters"]["scan_command1"])
        mask = [index > 0 for index in range(len(grid))]
        return {"would_queue": True, "blockers": [], "eta": {"estimated_seconds": 5.0},
                "requested_points": len(grid), "feasible_points": sum(mask),
                "planned_feasible_mask": mask, "partial": True}

    def submit_scan(self, body, idempotency_key=None):
        raise AssertionError("a partially-infeasible grid must never be submitted")


class _SingleFeasiblePointTavi(MockTavi):
    """Only one point of any requested grid is reachable — below the trim floor, so undoable."""

    def validate(self, body):
        grid = _parse_grid(body["parameters"]["scan_command1"])
        mask = [index == 0 for index in range(len(grid))]
        return {"would_queue": True, "blockers": ["a2_out_of_range"],
                "eta": {"estimated_seconds": 5.0},
                "requested_points": len(grid), "feasible_points": 1,
                "planned_feasible_mask": mask, "partial": True}

    def submit_scan(self, body, idempotency_key=None):
        raise AssertionError("a one-point window must never be submitted")


class _InfeasibleAtSubmitTavi(MockTavi):
    """Validation says the grid is fine; the instrument then refuses it as partially infeasible."""

    def submit_scan(self, body, idempotency_key=None):
        raise TaviError(400, "POST /scan -> HTTP 400", _INFEASIBLE_POINTS_BODY)


class _ExecutedGridMismatchTavi(MockTavi):
    """An instrument that executes one point fewer than the grid it was handed."""

    def get_scan_data(self, job_id):
        document = super().get_scan_data(job_id)
        result = document["result"]
        result["scan_values_1"] = result["scan_values_1"][:-1]
        result["counts"] = result["counts"][:-1]
        return document


class _TargetOutsideLongestRunTavi(MockTavi):
    """The reachable stretch and the physics are in different places.

    A wide high-energy band is reachable while the peak sits in a short low-energy island, so
    maximizing run length alone would submit a long, cheap, perfectly feasible scan of nothing.
    """

    def __init__(self, omega_true, *, island_fraction=0.10, gap_fraction=0.25):
        super().__init__(omega_true)
        self.island_fraction = float(island_fraction)
        self.gap_fraction = float(gap_fraction)
        self.masks: list[list[bool]] = []
        self.island_half_width: float | None = None
        self.gap: float | None = None

    def _mask(self, grid, peak):
        # A short island around the peak, then an infeasible gap, then everything above is reachable
        # — so the longest run is the high-energy band that cannot see the excitation at all. The
        # geometry only exists where the peak does: the EIG planner sizes its own windows and reaches
        # this one only after a few designs, and a window that does not contain the excitation at all
        # is an ordinary, fully-reachable scan, not an infeasible one.
        if peak < grid[0] or peak > grid[-1]:
            return [True] * len(grid)
        if self.island_half_width is None:
            span = grid[-1] - grid[0]
            self.island_half_width = self.island_fraction * span
            self.gap = self.gap_fraction * span
        return [abs(energy - peak) <= self.island_half_width or energy >= peak + self.gap
                for energy in grid]

    def validate(self, body):
        grid = _parse_grid(body["parameters"]["scan_command1"])
        mask = self._mask(grid, self.omega_true(body["parameters"]["H"]))
        self.masks.append(mask)
        if all(mask):
            return super().validate(body)
        return {"would_queue": True, "blockers": [], "eta": {"estimated_seconds": 5.0},
                "requested_points": len(grid), "feasible_points": sum(mask),
                "planned_feasible_mask": mask, "partial": True}

    def submit_scan(self, body, idempotency_key=None):
        grid = _parse_grid(body["parameters"]["scan_command1"])
        if not all(self._mask(grid, self.omega_true(body["parameters"]["H"]))):
            raise TaviError(400, "POST /scan -> HTTP 400", _INFEASIBLE_POINTS_BODY)
        return super().submit_scan(body, idempotency_key=idempotency_key)


class _TargetUnreachableTavi(MockTavi):
    """Only energies far above the peak are reachable — no run can see the excitation."""

    def __init__(self, omega_true, *, reachable_fraction=0.4):
        super().__init__(omega_true)
        self.reachable_fraction = float(reachable_fraction)
        self.reachable_above: float | None = None

    def validate(self, body):
        grid = _parse_grid(body["parameters"]["scan_command1"])
        peak = self.omega_true(body["parameters"]["H"])
        if peak < grid[0] or peak > grid[-1]:
            # A window that does not contain the excitation is an ordinary, fully reachable scan;
            # the geometry this fixture models only exists where the peak does.
            return super().validate(body)
        if self.reachable_above is None:
            # Pinned inside the FIRST window that holds the peak, above it, so every reachable run
            # is real and none of them can see the excitation.
            self.reachable_above = peak + self.reachable_fraction * (grid[-1] - peak)
        mask = [energy >= self.reachable_above for energy in grid]
        if all(mask) or not any(mask):
            return {"would_queue": bool(all(mask)), "blockers": [] if all(mask) else ["infeasible"],
                    "eta": {"estimated_seconds": 5.0},
                    "requested_points": len(grid), "feasible_points": sum(mask),
                    "planned_feasible_mask": mask}
        return {"would_queue": True, "blockers": [], "eta": {"estimated_seconds": 5.0},
                "requested_points": len(grid), "feasible_points": sum(mask),
                "planned_feasible_mask": mask, "partial": True}

    def submit_scan(self, body, idempotency_key=None):
        # A window the ladder later re-centred entirely into the reachable band is fully feasible
        # and legitimately submittable; only a partially-infeasible grid is refused, as TAVI does.
        grid = _parse_grid(body["parameters"]["scan_command1"])
        if (self.reachable_above is not None
                and not all(energy >= self.reachable_above for energy in grid)
                and grid[0] <= self.omega_true(body["parameters"]["H"]) <= grid[-1]):
            raise TaviError(400, "POST /scan -> HTTP 400", _INFEASIBLE_POINTS_BODY)
        return super().submit_scan(body, idempotency_key=idempotency_key)


class _NotQueueablePartialTavi(MockTavi):
    """A transient refusal (full queue) that happens to carry a partial feasibility mask."""

    def __init__(self, omega_true):
        super().__init__(omega_true)
        self.validated_windows: list[tuple[float, float]] = []

    def validate(self, body):
        grid = _parse_grid(body["parameters"]["scan_command1"])
        _axis, lo, hi, _step = body["parameters"]["scan_command1"].split()
        self.validated_windows.append((round(float(lo), 6), round(float(hi), 6)))
        mask = [index > 0 for index in range(len(grid))]
        return {"would_queue": False, "blockers": ["queue_full: 10 of 10 jobs pending"],
                "eta": {"estimated_seconds": 5.0},
                "requested_points": len(grid), "feasible_points": sum(mask),
                "planned_feasible_mask": mask, "partial": True}

    def submit_scan(self, body, idempotency_key=None):
        raise AssertionError("a refused validation must never reach submit")


def _shadow_objective():
    """A single-q optic objective for the window-trimming/feasibility-repair tests below.

    None of its three callers exercise scan-window EIG at all (they are about
    feasibility-trim/decline mechanics) — "off" keeps the campaign to the one validate-per-attempt
    shape those tests assert on; "authoritative" would additionally run the O41 target-core
    validation on every attempt (SCENE-4 PR 4.4, D3 retired the middle "shadow" mode that used to
    let scan-design/realized-comparison machinery run without also invoking O41 selection).
    """
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    return ObjectiveSpec(
        path=[(2.0, 0.0, 0.0)], tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e12}, target_branch="optic",
        energy_search_meV=(-2.0, 12.0), **frozen)


def test_a_partially_infeasible_window_is_trimmed_and_submitted(tmp_path):
    """The repair. TAVI reported 54 of 55 points feasible, the loop submitted the full grid anyway,
    the instrument 400'd it, and the campaign executed zero scans. A partially-reachable window is
    now trimmed to its largest contiguous feasible run and submitted."""
    omega = lambda _h: 6.0
    client = _PartialFeasibilityTavi(omega)
    summary, _workspace = _run(
        tmp_path, _shadow_objective(), client, make_mock_runner(omega, branch="optic"),
        stop_after=1)
    events = read_log(summary["session_log"])

    assert client.jobs, "the trimmed grid must reach the instrument"
    requested = client.validated_grids[0]
    submitted = _parse_grid(
        next(iter(client.jobs.values()))["parameters"]["scan_command1"])
    assert min(submitted) > client.min_feasible_energy, \
        "the infeasible edge energy is still in the submitted grid"
    assert len(submitted) == len(requested) - 1

    trim = next(event["payload"] for event in events if event["kind"] == "feasibility_trim")
    assert trim["dropped_point_count"] == 1
    # The wire renders %g (six significant digits), so the parsed grid matches the designed
    # window to about a milli-meV rather than exactly.
    assert trim["old_e_lo"] == pytest.approx(requested[0], abs=1e-3)
    assert trim["new_e_lo"] == pytest.approx(requested[1], abs=1e-3)
    # The trimmed bounds are the feasible run's own endpoints, so e_hi lands on the last *emitted*
    # energy rather than the designed ceiling the emitted grid never reached.
    assert trim["new_e_hi"] == pytest.approx(requested[-1], abs=1e-3)
    assert trim["old_e_hi"] >= requested[-1] - 1e-3
    assert trim["feasible_run"][0] == 1
    assert trim["planned_feasible_mask"][0] is False

    assert trim["previous_request_fingerprint"] != trim["request_fingerprint"]

    # The request that was finally validated is the request that was submitted.
    intent = next(event["payload"] for event in events
                  if event["kind"] == "submission_intent")
    validations = [event["payload"] for event in events if event["kind"] == "validate"]
    assert validations[-1]["request_fingerprint"] == intent["proposal_fingerprint"]
    assert validations[-1]["feasible_points"] == validations[-1]["requested_points"]

    # Partial infeasibility must never cost the hkl.
    assert not [event for event in events if event["kind"] == "action_suppressed"
                and event["payload"]["scope"] == "geometry"]


def test_a_window_with_one_feasible_point_is_declined_not_trimmed(tmp_path):
    """A one-point window is a degenerate window and the instrument 400s it, so the trim floor must
    decline rather than manufacture an unusable scan — but only request-scoped: a shifted window at
    the same q may still be fully measurable, so the hkl must not be condemned as geometry."""
    omega = lambda h: (h - 2.0) * 2.0
    summary, _workspace = _run(
        tmp_path, _objective(), _SingleFeasiblePointTavi(omega), make_mock_runner(omega))
    events = read_log(summary["session_log"])

    assert not [event for event in events if event["kind"] == "feasibility_trim"]
    below = [event["payload"] for event in events if event["kind"] == "exception"
             and event["payload"].get("action") == "feasible_run_below_minimum"]
    assert below, "a sub-minimum feasible run must be recorded"
    assert below[0]["reason"] == "largest_feasible_run_below_minimum"
    assert below[0]["feasible_run_points"] == 1
    suppressed = [event["payload"] for event in events if event["kind"] == "action_suppressed"]
    assert suppressed and suppressed[0]["verdict"] == "validation_rejected"
    assert all(event["payload"].get("scope") != "geometry" for event in events
               if event["kind"] == "action_suppressed")


def test_a_second_partial_verdict_after_a_trim_refuses_only_the_request(tmp_path):
    """One trim per proposal. An instrument whose answer never converges must cost the request, not
    the hkl, and must not spin the loop."""
    omega = lambda h: (h - 2.0) * 2.0
    summary, _workspace = _run(
        tmp_path, _objective(), _FirstPointInfeasibleTavi(omega), make_mock_runner(omega),
        stop_after=2)
    events = read_log(summary["session_log"])

    assert [event for event in events if event["kind"] == "feasibility_trim"]
    assert [event["payload"] for event in events if event["kind"] == "exception"
            and event["payload"].get("action") == "feasibility_trim_did_not_converge"]
    suppressed = [event["payload"] for event in events if event["kind"] == "action_suppressed"]
    assert suppressed, "the refused request must be recorded"
    assert {entry["scope"] for entry in suppressed} == {"request"}
    assert {entry["verdict"] for entry in suppressed} == {"validation_rejected"}
    assert summary["stop"]["state"] in {"met", "incomplete", "operator_stopped"}


def test_infeasible_points_at_submit_costs_the_request_not_the_hkl(tmp_path):
    """The backstop for a manifest that did not localize: the campaign records the refusal and keeps
    going instead of aborting on an unhandled TaviError."""
    omega = lambda h: (h - 2.0) * 2.0
    summary, _workspace = _run(
        tmp_path, _objective(), _InfeasibleAtSubmitTavi(omega), make_mock_runner(omega),
        stop_after=2)
    events = read_log(summary["session_log"])

    refusals = [event["payload"] for event in events if event["kind"] == "exception"
                and event["payload"].get("action") == "infeasible_points_at_submit"]
    assert refusals and refusals[0]["severity"] == "high"
    suppressed = [event["payload"] for event in events if event["kind"] == "action_suppressed"]
    assert suppressed
    assert {entry["scope"] for entry in suppressed} == {"request"}
    assert {entry["verdict"] for entry in suppressed} == {"infeasible_points_at_submit"}
    assert summary["stop"]["state"] in {"met", "incomplete", "operator_stopped"}
    rejection = loop_mod.classify_rejection("infeasible_points_at_submit")
    assert rejection.scope == "request"
    assert rejection.rejection_class.value == "structural"


def test_the_trim_keeps_the_run_containing_the_target_not_the_longest_one(tmp_path):
    """A trim that only maximizes run length can submit a long, cheap, feasible scan of nothing.

    Repro shape from review: the peak sits at 6 meV in a short low-energy island while a wide band
    above it is reachable. The kept window must be the island."""
    omega = lambda _h: 6.0
    client = _TargetOutsideLongestRunTavi(omega)
    summary, _workspace = _run(
        tmp_path, _shadow_objective(), client, make_mock_runner(omega, branch="optic"))
    events = read_log(summary["session_log"])

    trim = next(event["payload"] for event in events if event["kind"] == "feasibility_trim")
    assert trim["run_selection"] == "contains_target"
    assert trim["new_e_lo"] <= trim["target_energy"] <= trim["new_e_hi"]
    # The rejected alternative: a longer, entirely feasible, entirely useless high-energy window.
    mask = next(m for m in client.masks if not all(m))
    assert sum(mask) - (trim["feasible_run"][1] - trim["feasible_run"][0] + 1) > 0, \
        "the repro needs a longer feasible run outside the target island"
    assert client.jobs, "the trimmed window must still be submitted"
    # The trimmed grid specifically: the campaign's other jobs are ordinary scans of windows that
    # never held the excitation, which this fixture reports as fully reachable.
    submitted = next(grid for grid in
                     (_parse_grid(job["parameters"]["scan_command1"])
                      for job in client.jobs.values())
                     if abs(grid[0] - trim["new_e_lo"]) < 1e-3)
    assert submitted[0] <= 6.0 <= submitted[-1], \
        "the submitted window dropped the excitation it exists to measure"


def test_a_window_whose_reachable_runs_all_miss_the_target_is_declined(tmp_path):
    """No reachable stretch can see the peak, so there is nothing worth submitting here — and the
    refusal is request-scoped, leaving the widen/re-center ladder open at that q."""
    omega = lambda _h: 6.0
    summary, _workspace = _run(
        tmp_path, _shadow_objective(), _TargetUnreachableTavi(omega),
        make_mock_runner(omega, branch="optic"))
    events = read_log(summary["session_log"])

    excluded = [event["payload"] for event in events if event["kind"] == "exception"
                and event["payload"].get("action") == "feasible_run_excludes_target"]
    assert excluded, "a window that cannot see the target must say so"
    assert excluded[0]["reason"] == "no_feasible_run_overlaps_the_declared_target"
    assert not [event for event in events if event["kind"] == "feasibility_trim"]
    suppressed = [event["payload"] for event in events if event["kind"] == "action_suppressed"]
    assert suppressed
    assert any(entry["scope"] == "request" for entry in suppressed),         "the widen/re-center ladder stays open, so the refusal must be charged to the request"
    # A geometry-shaped refusal condemns this window at every exposure (commit e36e220), so it is
    # also charged there — naming the window it condemns rather than the whole hkl.
    geometry = [entry for entry in suppressed if entry["scope"] == "geometry"]
    assert all(entry["design_keys"] for entry in geometry),         "a geometry-scoped suppression must name the window it condemns"


def test_a_transient_refusal_carrying_a_partial_mask_does_not_move_the_window(tmp_path):
    """A full queue is not a geometry statement. Trimming on it would rewrite the request while
    answering none of the actual blocker, and lose the original window in the process."""
    omega = lambda h: (h - 2.0) * 2.0
    client = _NotQueueablePartialTavi(omega)
    summary, _workspace = _run(tmp_path, _objective(), client, make_mock_runner(omega))
    events = read_log(summary["session_log"])

    assert not [event for event in events if event["kind"] == "feasibility_trim"]
    assert not [event for event in events if event["kind"] == "exception"
                and event["payload"].get("action") in {"feasible_run_below_minimum",
                                                       "feasible_run_excludes_target",
                                                       "feasibility_trim_did_not_converge"}], \
        "a transient refusal must take the ordinary validation-rejected path"
    assert not client.jobs
    # No window the instrument saw differs from a window the planner proposed: nothing was mutated.
    proposed = {(round(event["payload"]["e_lo"], 6), round(event["payload"]["e_hi"], 6))
                for event in events if event["kind"] == "propose"}
    assert proposed
    assert client.validated_windows, "the campaign must still have validated something"
    assert all(any(abs(lo - p_lo) < 1e-3 and abs(hi - p_hi) < 1e-3 for p_lo, p_hi in proposed)
               for lo, hi in client.validated_windows)
    verdicts = {event["payload"]["verdict"] for event in events
                if event["kind"] == "action_suppressed"}
    assert verdicts == {"validation_rejected"}


def test_a_trimmed_then_declined_request_does_not_re_emit_next_iteration(tmp_path):
    """The trim re-fingerprints the proposal, so a decline afterwards charges only the trimmed
    request. Without also charging the pre-trim original the selector re-emits it every iteration
    and repeats trim -> decline forever."""
    omega = lambda h: (h - 2.0) * 2.0
    client = _FirstPointInfeasibleTavi(omega)
    summary, _workspace = _run(tmp_path, _objective(), client, make_mock_runner(omega))
    events = read_log(summary["session_log"])

    trims = [event["payload"] for event in events if event["kind"] == "feasibility_trim"]
    assert trims, "the first partial verdict must still trim"
    suppressed = [event["payload"] for event in events if event["kind"] == "action_suppressed"]
    assert suppressed
    charged = [entry for entry in suppressed if entry["superseded_request_fingerprints"]]
    assert charged, "the pre-trim request must be charged alongside the trimmed one"
    for entry in charged:
        assert entry["request_fingerprint"] not in entry["superseded_request_fingerprints"]
    # The churn signature is the SAME pre-trim REQUEST coming back. A different request at the same
    # q — a wider window, a production escalation — is the ladder doing its job and stays allowed.
    charged_fingerprints = {value for entry in suppressed
                            for value in entry["superseded_request_fingerprints"]}
    pre_trim = collections.Counter(entry["previous_request_fingerprint"] for entry in trims)
    assert max(pre_trim.values()) == 1, \
        f"a declined request was re-emitted and re-trimmed: {pre_trim}"
    assert set(pre_trim) <= charged_fingerprints, \
        "every trimmed-then-declined original must be suppressed, or it re-emits next iteration"


def test_suppression_rebuild_recovers_superseded_request_fingerprints(tmp_path):
    """Resume rebuilds suppression from the log, so the superseded original has to survive there."""
    log_path = tmp_path / "session.jsonl"
    log_event(log_path, "action_suppressed", {
        "scope": "request", "verdict": "validation_rejected", "rejection_class": "structural",
        "reason": "validation_rejected at hkl=[2.1, 0.0, 0.0]",
        "request_fingerprint": "b" * 64,
        "superseded_request_fingerprints": ["a" * 64],
        "hkl": [2.1, 0.0, 0.0], "iter": 1})
    _by_action, _by_hkl, by_request, _by_design = loop_mod.suppressed_actions(log_path)
    assert set(by_request) == {"a" * 64, "b" * 64}


def test_feasibility_shaped_refusal_recognises_geometry_and_nothing_else():
    assert wire._is_feasibility_shaped_refusal({"blockers": ["a2_out_of_range"]})
    assert wire._is_feasibility_shaped_refusal(
        {"blockers": ["physical_infeasible", "scattering triangle does not close"]})
    assert not wire._is_feasibility_shaped_refusal(
        {"blockers": ["limit_exceeded: queue neutron budget exceeded"]})
    # Mixed verdicts fail closed: one non-geometry blocker is enough to leave the window alone.
    assert not wire._is_feasibility_shaped_refusal(
        {"blockers": ["a2_out_of_range", "queue_full"]})
    assert not wire._is_feasibility_shaped_refusal({"blockers": []})


def test_select_feasible_run_prefers_the_target_and_refuses_a_blind_window():
    energies = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    manifest = {"planned_feasible_mask": [True, False, True, True, True, True]}
    # A one-point island containing the target beats a four-point run that does not.
    assert wire._select_feasible_run(
        manifest, energies, target_energy=0.0, acceptance=(-0.5, 0.5)) == ((0, 0),
                                                                          "contains_target")
    # No target declared → longest run, unchanged behaviour for survey/legacy proposals.
    assert wire._select_feasible_run(
        manifest, energies, target_energy=None, acceptance=None) == ((2, 5), "longest")
    # Target in the infeasible gap and the longest run does not reach it → refuse.
    assert wire._select_feasible_run(
        manifest, energies, target_energy=1.0, acceptance=(0.9, 1.1)) == (None, "target_excluded")
    # Same target, but the acceptance interval reaches the feasible run → longest run is enough.
    assert wire._select_feasible_run(
        manifest, energies, target_energy=1.0, acceptance=(0.5, 2.5)) == ((2, 5), "longest")


def test_largest_feasible_run_refuses_a_manifest_that_does_not_match_the_request():
    """A mask of the wrong length describes a different grid; trimming against it would move the
    window to the wrong energies, so it must read as 'no localization'."""
    assert wire._largest_feasible_run({"planned_feasible_mask": [True] * 4}, 5) is None
    assert wire._largest_feasible_run(
        {"feasible_segments": [{"start_index": 1, "end_index": 9}]}, 5) is None
    assert wire._largest_feasible_run({"planned_feasible_mask": [False] * 5}, 5) is None
    assert wire._largest_feasible_run({}, 5) is None
    # Longest run wins, and the segment form agrees with the mask form.
    assert wire._largest_feasible_run(
        {"planned_feasible_mask": [True, False, True, True, True]}, 5) == (2, 4)
    assert wire._largest_feasible_run(
        {"feasible_segments": [{"start_index": 2, "end_index": 4}]}, 5) == (2, 4)


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


def test_pending_anchor_escalations_names_within_bar_masters_that_are_not_folds(tmp_path):
    """Guards the belief-anchor-starvation fix (HANDOFF-eig.md 7.29/7.30): a prize point the planner
    has stopped revisiting (``within_bar``) whose pooled master is not yet a ``fold`` fit must be named
    for escalation, since the dispersion prior admits only folds and the fast tier never produces
    one — whether its HESSE interval is trusted or not (the first version stopped at "trusted" and
    the anchor chain broke at the first trusted fast fit, live, h=2.50). A point that is already a
    fold, or one the planner is still actively scanning (not ``within_bar``), must not be named."""
    report = tmp_path / "report"
    report.mkdir(parents=True)
    cols = ["scan", "detector", "h", "k", "l", "status", "verdict", "disposition", "confidence",
            "omega0", "interval_lo", "interval_hi", "interval_reliable",
            "hesse_lo", "hesse_hi", "hesse_trusted", "pooled", "modality", "scan_id"]
    rows = [
        # within_bar, fast/untrusted -> named.
        {"scan": 1, "detector": "det", "h": 2.7, "k": 0.0, "l": 0.0, "status": "fitted",
         "verdict": "resolved", "disposition": "resolved", "omega0": 4.88,
         "hesse_lo": 4.6, "hesse_hi": 5.2, "hesse_trusted": False, "interval_reliable": False,
         "pooled": True, "modality": "unpooled", "scan_id": "master:tavi_(54, 0, 0)_200.0"},
        # within_bar, fast/TRUSTED -> still named: trusted is not folded.
        {"scan": 2, "detector": "det", "h": 2.5, "k": 0.0, "l": 0.0, "status": "fitted",
         "verdict": "resolved", "disposition": "resolved", "omega0": 4.26,
         "hesse_lo": 4.2, "hesse_hi": 4.32, "hesse_trusted": True, "interval_reliable": False,
         "pooled": True, "modality": "unpooled", "scan_id": "master:tavi_(50, 0, 0)_200.0"},
        # within_bar, already a fold master -> NOT named.
        {"scan": 3, "detector": "det", "h": 2.3, "k": 0.0, "l": 0.0, "status": "fitted",
         "verdict": "resolved", "disposition": "resolved", "omega0": 2.73,
         "interval_lo": 2.71, "interval_hi": 2.75, "interval_reliable": True,
         "pooled": True, "modality": "fold", "scan_id": "master:tavi_(46, 0, 0)_200.0"},
        # still above the bar (not within_bar) -> NOT named.
        {"scan": 4, "detector": "det", "h": 3.0, "k": 0.0, "l": 0.0, "status": "fitted",
         "verdict": "resolved", "disposition": "resolved", "omega0": 6.0,
         "hesse_lo": 5.5, "hesse_hi": 6.5, "hesse_trusted": False, "interval_reliable": False,
         "pooled": True, "modality": "unpooled", "scan_id": "master:tavi_(60, 0, 0)_200.0"},
    ]
    with open(report / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) for c in cols})

    def point(hkl, within_bar, exempt=False):
        return types.SimpleNamespace(hkl=hkl, exempt=exempt, within_bar=within_bar)

    plan_decision = types.SimpleNamespace(per_point=[
        point((2.7, 0.0, 0.0), within_bar=True),
        point((2.5, 0.0, 0.0), within_bar=True),
        point((2.3, 0.0, 0.0), within_bar=True),
        point((3.0, 0.0, 0.0), within_bar=False),
    ])
    assert (_pending_anchor_escalations(tmp_path, plan_decision)
            == ["master:tavi_(54, 0, 0)_200.0", "master:tavi_(50, 0, 0)_200.0"])
    # An exempt point (the untestable zone-centre point) is never a candidate either.
    plan_decision.per_point[0].exempt = True
    assert _pending_anchor_escalations(tmp_path, plan_decision) == ["master:tavi_(50, 0, 0)_200.0"]


def test_unfolded_master_leaves_a_fork_alone():
    """A fork is the full tier's own answer (two answers); refitting cannot make it a fold, so it is
    never named — otherwise the same escalation would be bought on every iteration."""
    rows = [{"h": 2.7, "k": 0.0, "l": 0.0, "pooled": True, "disposition": "resolved",
             "omega0": 4.88, "modality": "fork", "scan_id": "master:tavi_(54, 0, 0)_200.0"}]
    assert _unfolded_master(rows, (2.7, 0.0, 0.0)) is None


def test_planner_candidates_bind_the_certifying_halfwidth_to_the_selected_candidate():
    """PR #37 review P1: the master's certifying half-width is the primary mode's model-averaged
    interval, located by the row's ``omega0``; a secondary mode gets only "detected, precision
    unknown", and a row that publishes no ``omega0`` binds it to nothing — otherwise a peak the
    interval never described carried the primary mode's precision into the prize. Not gated on the
    session tracker's state: that re-created a stall at the near-fold point, live."""
    path = [(2.7, 0.0, 0.0)]
    domain = (-9.0, 9.0)
    row = {"hkl": (2.7, 0.0, 0.0), "halfwidth": 0.05, "certifiable": True, "modality": "fold",
           "pool_consistent": True, "omega0": 4.881, "track_state": "ambiguous",
           "branch_candidates": [{"energy_meV": 4.88}, {"energy_meV": 6.90}]}
    cands = _planner_candidates([row], path, domain)[0]
    assert cands[0].sigma_meV == pytest.approx(0.05)
    assert cands[1].sigma_meV == pytest.approx(4.5)          # half the positive domain
    # the interval follows the mode it describes, whichever the tracker later selected
    cands = _planner_candidates([dict(row, omega0=6.90)], path, domain)[0]
    assert (cands[0].sigma_meV, cands[1].sigma_meV) == (pytest.approx(4.5), pytest.approx(0.05))
    cands = _planner_candidates([dict(row, omega0=None)], path, domain)[0]
    assert all(c.sigma_meV == pytest.approx(4.5) for c in cands)


def test_candidate_probabilities_label_only_the_selected_mode():
    """PR #37 review P1: a physical row's ``branch_id`` labels the mode the tracker selected, not
    every fitted mode — a two-mode fit's second lobe must not enter the acoustic brightness."""
    row = {"branch_id": "acoustic", "selected_candidate_index": 0}
    branches = ("acoustic", "optic")
    assert _candidate_probabilities({}, row, branches, 0) == {"acoustic": 1.0, "optic": 0.0}
    assert _candidate_probabilities({}, row, branches, 1) == {}
    # explicit per-candidate probabilities (a pooled master) always win
    assert _candidate_probabilities({"probabilities": {"optic": 0.9, "acoustic": 0.1}}, row,
                                    branches, 1) == {"acoustic": 0.1, "optic": 0.9}


def test_slice_rows_keep_only_the_campaign_condition():
    """PR #37 review P1: the planner's world is one (sample, temperature bin); a reused workspace
    holding the same Q at another temperature or sample must not feed it. Rows without a stamp are
    kept — there is nothing to disagree with."""
    rows = [{"hkl": (2.7, 0, 0), "sample": "tavi", "T": 200.4},
            {"hkl": (2.7, 0, 0), "sample": "tavi", "T": 300.0},
            {"hkl": (2.7, 0, 0), "sample": "other", "T": 200.0},
            {"hkl": (2.7, 0, 0)}]
    kept = _slice_rows(rows, "tavi", 200.0, 1.0)
    assert [r.get("T") for r in kept] == [200.4, None]
    assert len(_slice_rows(rows, None, None, None)) == 4
    # One sample in the evidence IS the campaign's sample, whatever the driver calls it: before the
    # first belief row the resolved name is the driver's default, and filtering on it dropped every
    # master (live, h=2.05 for ever).
    one_sample = [r for r in rows if r.get("sample") != "other"]
    assert len(_slice_rows(one_sample, "Al_phonon_DFT", None, 1.0)) == 3


def test_unfolded_master_ignores_individual_scan_rows():
    """An individual (unpooled) scan's own fit must not count — only the pooled master's fit decides
    whether a q's evidence is durable."""
    rows = [{"h": 2.7, "k": 0.0, "l": 0.0, "pooled": False, "disposition": "resolved",
             "omega0": 4.88, "modality": "unpooled", "scan_id": "tavi:501:det"}]
    assert _unfolded_master(rows, (2.7, 0.0, 0.0)) is None


# --------------------------------------------------------------------------- crash-safe resume (Part C)
def test_proposal_from_submission_preserves_auxiliary_action_identity():
    """A crashed-and-resumed auxiliary probe must keep its sink and action identity. Without the sink
    the recovered scan becomes canonical by default and pools into the certified master; without the
    scientific/feasibility fingerprints it loses its audit identity. This is load-bearing once the
    clean-null evidence lands on the auxiliary node."""
    from isar.schema.steering import AUXILIARY_TRACK_NODE
    payload = {
        "hkl": [2.9, 0.0, 0.0], "kind": "identity_rival_probe", "acquisition_id": "run:00000007",
        "submitted_ncount": 10_000_000, "n_points": 5, "acquisition_target_energy": 7.115,
        "evidence_sink": AUXILIARY_TRACK_NODE,
        "scientific_action_fingerprint": "sci-fp-123",
        "feasibility_manifest_fingerprint": "feas-fp-456",
        "proposal_fingerprint": "req-fp-789",
    }
    proposal = _proposal_from_submission(payload)

    assert proposal.meta["evidence_sink"] == AUXILIARY_TRACK_NODE
    assert proposal.meta["scientific_action_fingerprint"] == "sci-fp-123"
    assert proposal.meta["feasibility_manifest_fingerprint"] == "feas-fp-456"


def test_proposal_from_submission_defaults_a_missing_sink_to_canonical():
    """A legacy submission with no persisted sink recovers as an ordinary canonical scan."""
    from isar.schema.steering import CANONICAL_MASTER
    proposal = _proposal_from_submission({"hkl": [2.0, 0.0, 0.0], "kind": "production"})
    assert proposal.meta["evidence_sink"] == CANONICAL_MASTER


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
        "search_energy_lo": -0.8, "search_energy_hi": 1.2,
        "search_energy_step": 0.1,
        "proposal_fingerprint": "request-recovered",
        "scan_design_inventory_ref": {
            "kind": "scan_design_inventory",
            "sha256": "inventory-recovered",
            "path": "report/scan-design/inventory-recovered.json",
        },
        "scan_design_candidate_id": "candidate-recovered",
        "scan_design_context_fingerprint": "context-recovered",
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


_RECOVERY_STOP_TOKEN = "recovery-bound"


def _recovery_config(ws, client, omega):
    """A campaign config that performs crash recovery and then acquires nothing.

    The loop has no iteration cap to set to zero, so the bound is a cooperative operator stop that
    is already present at the first iteration boundary — the supported way to say "recover, then
    stop". Recovery itself runs before the loop, so every recovery event still appears.
    """
    control = Path(ws) / "report" / "control"
    control.mkdir(parents=True, exist_ok=True)
    (control / f"stop-{_RECOVERY_STOP_TOKEN}.json").write_text(
        json.dumps({"requested_by": "test", "reason": "recover only"}), encoding="utf-8")
    return {"workspace": ws, "client": client, "run_isar": make_mock_runner(omega),
            "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8},
            "seed": 7, "control_token": _RECOVERY_STOP_TOKEN}


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
    shutil.copy(
        Path(__file__).resolve().parents[2]
        / "isar" / "parsers" / "cards" / "tavi.policy.toml",
        ws / "card.policy.toml",
    )
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
    state = json.loads((ws / "card.state.json").read_text(encoding="utf-8"))
    provenance = next(
        item for item in state["acquisition_provenance"]
        if item["job_id"] == "old-job"
    )
    assert provenance["proposal_fingerprint"] == "request-recovered"
    assert provenance["scan_design_inventory_ref"] == _payload[
        "scan_design_inventory_ref"]
    assert provenance["scan_design_candidate_id"] == "candidate-recovered"
    assert provenance["scan_design_context_fingerprint"] == "context-recovered"
    assert provenance["search_energy_lo"] == -0.8
    assert provenance["search_energy_hi"] == 1.2
    assert provenance["search_energy_step"] == 0.1
    assert client.submit_calls == 0


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
        _objective(), _recovery_config(ws, client, lambda _h: 0.2))
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
    for expected in ("stop_eval", "propose", "validate", "submit",
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
                        kind=kind, reason="test",
                        n_points=min(_emitted_point_count(e_lo, e_hi, e_step), MAX_POINTS))


def test_emitted_point_count_is_unclamped():
    # The TRUE count TAVI computes from the command, NOT clamped to any budget: a wide window with a
    # tight step is exactly the over-limit case (the documented 594-pt scan came from a widened
    # window keeping a ~0.05 meV step). The guard is what clamps it, and it is tested next.
    assert _emitted_point_count(-2.0, 27.65, 0.05) > DEFAULT_MAX_POINTS   # ~594, unclamped
    assert _emitted_point_count(-2.0, 27.65, 0.05) > MAX_POINTS


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
    # The point ceiling still binds and coarsens the step; the exposure is left exactly as the
    # planner asked for it — no floor raises it and no clamp lowers it while the budget affords it.
    p.ncount = 50
    _finalize_proposal(
        p, objective, cfg, {"kind": "neutrons", "amount": 1_000.0, "spent": 0.0}, it=2)
    assert p.n_points == 10 and _emitted_point_count(p.e_lo, p.e_hi, p.e_step) == 10
    assert p.ncount == 50
    # A design the remaining budget cannot buy is refused outright rather than quietly downgraded
    # into a different, cheaper measurement: the planner ranked cheaper designs too.
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
    clamp = [e["payload"] for e in read_log(cfg.log_path)
             if (e.get("payload") or {}).get("action") == "proposal_ncount_clamped_to_limit"]
    assert len(clamp) == 1
    assert clamp[0]["requested_ncount"] == 400_000_000
    assert clamp[0]["effective_ncount"] == 100_000_000
    assert clamp[0]["n_points"] == 10

    # A tighter queue budget composes with the per-point limit and simply clamps further: there is
    # no exposure FLOOR to fall below any more, because the planner chose the exposure.
    cfg.instrument_limits["queue_neutron_budget"] = 900_000_000
    _finalize_proposal(
        p, objective, cfg, {"kind": "counting_seconds", "amount": 60.0, "spent": 0.0}, it=8)
    assert p.ncount == 90_000_000


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


class _RejectingTavi(MockTavi):
    def validate(self, body):
        return {"would_queue": False, "blockers": ["unknown refusal"],
                "requested_points": _cmd_points(body["parameters"]["scan_command1"]),
                "feasible_points": _cmd_points(body["parameters"]["scan_command1"])}


def test_validation_refusals_use_distinct_logical_acquisitions(tmp_path):
    """Every refused attempt is its own logical acquisition, and no *identical* request is re-sent.

    An instrument that refuses everything used to burn the whole iteration budget: the same refused
    request was re-emitted once per iteration, on the reasoning that an operational refusal is not
    evidence the q is infeasible. Only structural and budget verdicts ever reach the request
    suppression set, though — transient ones are scoped "none" — so each re-emission re-sent a request
    that had already been refused structurally. The ladder that answers the same question with a
    *different* window or exposure keeps its own fingerprint and stays open; when it too runs out, the
    campaign says so instead of spinning."""
    from isar.drive.acquisition.inventory import TERMINAL_CONTROLLER_FAULT

    client = _RejectingTavi()
    summary, _ws = _run(
        tmp_path, _objective(), client, make_mock_runner(lambda h: (h - 2.0) * 2.0))
    proposals = [e["payload"] for e in read_log(summary["session_log"])
                 if e["kind"] == "propose"]

    assert client.n == 0, "nothing was ever accepted, so nothing may be submitted"
    assert len({p["acquisition_id"] for p in proposals}) == len(proposals), (
        "each refused attempt is its own logical acquisition")
    requests = [(tuple(p["hkl"]), p["e_lo"], p["e_hi"], p["e_step"], p["ncount"])
                for p in proposals]
    assert len(set(requests)) == len(requests), "an identical refused request is never re-emitted"
    # A bound, not a target: the planner ranks every window against every exposure rung, so the
    # count moves with the ladder. What it pins is that the campaign exhausts a finite action space
    # and stops, rather than re-emitting forever until the iteration ceiling.
    assert len(proposals) < 60, "the iteration limit is no longer the guardrail"
    assert summary["stop"]["terminal_cause"] == loop_mod.TERMINAL_CONTROLLER_FAULT
    assert "already refused" in summary["stop"]["reasons"][0]


def _cmd_points(cmd):
    _, lo, hi, step = cmd.split()
    return _emitted_point_count(float(lo), float(hi), float(step))


def test_campaign_never_submits_over_limit_scan(tmp_path):
    # The instrument's advertised point limit binds every SUBMITTED command, however wide a window
    # the planner designs — the guard that stopped the 594-point over-limit scan. A wide belief at a
    # high ω routinely designs past a 50-point limit, so the clamp must also fire.
    omega = lambda h: 9.0
    client = _StateTavi(omega, max_points=50)
    summary, ws = _run(tmp_path, _objective(), client,
                       make_mock_runner(omega, window_gate=True))
    assert client.submitted_cmds
    assert all(_cmd_points(c) <= 50 for c in client.submitted_cmds), \
        [(_cmd_points(c), c) for c in client.submitted_cmds if _cmd_points(c) > 50]
    # The planner is handed the same live limit (``_runtime_limits``) and designs inside it, so the
    # guard is now belt-and-braces rather than the routine path — but when it does fire it must fire
    # against the instrument's advertised number, not a stale policy one.
    clamps = [e for e in read_log(summary["session_log"])
              if e["kind"] == "exception" and (e["payload"] or {}).get("action") == "proposal_clamped"]
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
        tmp_path, _objective(), _SubmitFailingTavi(omega), make_mock_runner(omega))

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
        tmp_path, _objective(), _WaitFailingTavi(omega), make_mock_runner(omega))

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
           "control_token": token}
    summary = run_campaign(_objective(), cfg)

    assert summary["stop"]["state"] == "operator_stopped"
    events = read_log(summary["session_log"])
    assert any(e["kind"] == "run" for e in events)              # the final iteration completed its scan
    acks = [e for e in events if e["kind"] == "decide"
            and (e.get("payload") or {}).get("event") == "operator_stop"]
    assert len(acks) == 1 and acks[0]["payload"]["iter"] == 1   # the next iteration boundary
    # Certification must not run on an operator stop, even on this path.
    assert not any((e.get("payload") or {}).get("event") == "certify_start"
                   for e in events if e["kind"] == "decide")
    stops = [e for e in events if e["kind"] == "stop"]
    assert len(stops) == 1 and events[-1]["kind"] == "stop"
    assert stops[0]["payload"]["state"] == "operator_stopped"


# --------------------------------------------------------------------------- budget-bounded loop
def test_counting_time_budget_terminates_a_neutron_unlimited_campaign(tmp_path):
    # There is no iteration cap: with the neutron budget unset (unlimited by operator choice) the
    # counting-time budget is the only thing that can stop an unconverged campaign, and it must.
    omega = lambda h: (h - 2.0) * 2.0
    objective = ObjectiveSpec.along_h(
        h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=0.001,       # never reachable
        budget={"kind": "counting_seconds", "amount": 12.0},         # MockTavi bills 5 s per scan
        name="time-bounded")
    assert objective.budget["kind"] == "counting_seconds"            # no neutron limit anywhere
    summary, _ws = _run(tmp_path, objective, MockTavi(omega), make_mock_runner(omega))
    assert summary["stop"]["state"] == "incomplete"
    assert any("budget exhausted" in reason for reason in summary["stop"]["reasons"]), \
        summary["stop"]["reasons"]
    assert summary["budget"]["spent"] >= 12.0


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


# --------------------------------------------------- selection + finalize as one bounded step (O29)
# GAP 1: an unaffordable / instrument-refused first action must not terminate a campaign while a
# cheaper eligible action is still affordable. Selection and finalization are unified in
# _next_finalized_proposal, which classifies a budget/instrument refusal and tries the next candidate
# instead of letting it escape or stop the loop. These exercise the helper directly with a fake plan
# and a monkeypatched _finalize_proposal (the full campaign harness is unnecessary weight for this).

class _FakePlan:
    """Hands back a fixed sequence of proposals, then None — the ordering ``next_proposal`` guarantees."""

    def __init__(self, proposals):
        self._proposals = list(proposals)
        self._i = 0

    def next_proposal(self):
        if self._i >= len(self._proposals):
            return None
        proposal = self._proposals[self._i]
        self._i += 1
        return proposal


def _fake_proposal(reason, **meta):
    return types.SimpleNamespace(
        h=2.1, k=0.0, l=0.0, e_lo=-2.0, e_hi=7.0, e_step=0.5, n_points=19, ncount=100,
        kind="production", reason=reason, acquisition_id=None, intent=None, meta=dict(meta))


def _helper_cfg(tmp_path):
    return types.SimpleNamespace(log_path=tmp_path / "log.jsonl", calibration=_UNITY_CAL, floor=None)


# ---------------------------------------------------------------------------
# Campaign background: frozen at compile time, stamped as transport (Stage 5)
# ---------------------------------------------------------------------------

_BG_SOURCE_IDS = (
    "environment_flat", "environment_slope", "environment_cosmic_spikes",
    "instrument_aluminum_powder", "sample_elastic", "sample_elastic_tail",
)
_CATALOG_V1_SOURCE_IDS = (
    "environment_flat", "environment_slope", "sample_diffuse",
    "sample_elastic", "sample_elastic_tail",
)


def _bg_spec(*, enabled=True, sources=None, catalog_version=2):
    return {"catalog_version": catalog_version, "enabled": enabled, "sources": sources or {}}


def _normalized_bg(*, enabled=True, sources=None, catalog_version=2,
                   source_ids=_BG_SOURCE_IDS):
    configured = sources or {}
    return {
        "catalog_version": catalog_version,
        "enabled": enabled,
        "sources": {
            source_id: json.loads(json.dumps(
                configured.get(source_id, {"enabled": False, "scale": 1.0})))
            for source_id in source_ids
        },
    }


class _BackgroundTavi(MockTavi):
    """A MockTavi that advertises the v2 source catalog and records every body it sees."""

    def __init__(self, omega_true=lambda h: (h - 2.0) * 2.0, *,
                 catalog_version=2, source_ids=_BG_SOURCE_IDS,
                 background_schema="tavi.background/2"):
        super().__init__(omega_true)
        self.catalog_version = catalog_version
        self.source_ids = tuple(source_ids)
        self.background_schema = background_schema
        self.validate_bodies = []
        self.set_background_calls = []
        self.schema_calls = 0

    def get_schema(self):
        self.schema_calls += 1
        return {"background": {
            "background_schema": self.background_schema,
            "catalog_version": self.catalog_version,
            "sources": {
                source_id: {"category": source_id.split("_", 1)[0], "label": source_id}
                for source_id in self.source_ids
            },
        }}

    def validate(self, body):
        self.validate_bodies.append(json.loads(json.dumps(body)))
        return super().validate(body)

    def set_background(self, spec):
        self.set_background_calls.append(spec)
        return {}


def _frozen_events(session_log):
    return [e["payload"] for e in read_log(session_log)
            if e["kind"] == "decide"
            and (e.get("payload") or {}).get("event") == "sample_background_frozen"]


def test_campaign_stamps_the_normalized_source_background_on_every_body(tmp_path):
    """The campaign's background is its sample declaration, frozen once and carried by every request —
    never read from TAVI's mutable config default at scan time."""
    omega = lambda h: (h - 2.0) * 2.0
    client = _BackgroundTavi(omega)
    summary, _ws = _run(tmp_path, _objective(), client, make_mock_runner(omega),
                        sample_background=_bg_spec(sources={
                            "instrument_aluminum_powder": {"enabled": True, "scale": 1.0},
                            "environment_cosmic_spikes": {"enabled": True, "scale": 0.5},
                        }))

    expected = _normalized_bg(sources={
        "instrument_aluminum_powder": {"enabled": True, "scale": 1.0},
        "environment_cosmic_spikes": {"enabled": True, "scale": 0.5},
    })
    assert client.jobs, "the campaign submitted no scans"
    for body in client.jobs.values():
        assert body["background"] == expected
        assert "background_source" not in body      # TAVI derives the source from the key's presence
    for body in client.validate_bodies:
        assert body["background"] == expected       # validate/submit parity

    frozen = _frozen_events(summary["session_log"])
    assert len(frozen) == 1
    assert frozen[0]["frozen"] == expected
    assert frozen[0]["spec"] == _bg_spec(sources={
        "instrument_aluminum_powder": {"enabled": True, "scale": 1.0},
        "environment_cosmic_spikes": {"enabled": True, "scale": 0.5},
    })
    assert frozen[0]["catalog_version"] == 2
    # Stamping, never config mutation; the source catalog is read once, at setup (the other
    # GET /schema is the campaign's instrument-limits preflight).
    assert client.set_background_calls == []
    assert client.schema_calls == 2


def test_campaign_independent_source_scales_survive_the_freeze(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = _BackgroundTavi(omega)
    summary, _ws = _run(tmp_path, _objective(), client, make_mock_runner(omega),
                        sample_background=_bg_spec(sources={
                            "environment_cosmic_spikes": {"enabled": True, "scale": 2.5},
                            "instrument_aluminum_powder": {"enabled": True, "scale": 1.25},
                            "sample_elastic_tail": {"enabled": False, "scale": 0.25},
                        }))

    expected = _normalized_bg(sources={
        "environment_cosmic_spikes": {"enabled": True, "scale": 2.5},
        "instrument_aluminum_powder": {"enabled": True, "scale": 1.25},
        "sample_elastic_tail": {"enabled": False, "scale": 0.25},
    })
    assert client.jobs and all(body["background"] == expected for body in client.jobs.values())
    frozen = _frozen_events(summary["session_log"])
    assert frozen[0]["frozen"]["sources"]["environment_cosmic_spikes"]["scale"] == 2.5
    assert frozen[0]["frozen"]["sources"]["instrument_aluminum_powder"]["scale"] == 1.25
    assert frozen[0]["frozen"]["sources"]["sample_elastic_tail"] == {
        "enabled": False, "scale": 0.25}


def test_campaign_can_declare_a_clean_instrument(tmp_path):
    """A disabled/none declaration stamps an empty override on every body — insulating the campaign
    from whatever profile the operator last left configured on the instrument."""
    omega = lambda h: (h - 2.0) * 2.0
    client = _BackgroundTavi(omega)
    summary, _ws = _run(tmp_path, _objective(), client, make_mock_runner(omega),
                        sample_background=_bg_spec(enabled=False))

    expected = _normalized_bg(enabled=False)
    assert client.jobs and all(body["background"] == expected for body in client.jobs.values())
    frozen = _frozen_events(summary["session_log"])
    assert len(frozen) == 1 and frozen[0]["frozen"] == expected
    assert client.set_background_calls == []


@pytest.mark.parametrize("scale", ["2", True, float("nan"), -1.0])
def test_campaign_background_invalid_scale_aborts_setup(tmp_path, scale):
    omega = lambda h: (h - 2.0) * 2.0
    client = _BackgroundTavi(omega)
    with pytest.raises(ValueError, match="'scale' must be a finite number"):
        _run(tmp_path, _objective(), client, make_mock_runner(omega),
             sample_background=_bg_spec(sources={
                 "environment_flat": {"enabled": True, "scale": scale}}))
    assert client.jobs == {}


def test_campaign_complete_background_form_passes_through_unchanged(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = _BackgroundTavi(omega)
    spec = _normalized_bg(sources={
        "instrument_aluminum_powder": {"enabled": True, "scale": 3.0},
    })
    summary, _ws = _run(tmp_path, _objective(), client, make_mock_runner(omega),
                        sample_background=json.loads(json.dumps(spec)))

    assert client.jobs and all(body["background"] == spec for body in client.jobs.values())
    assert _frozen_events(summary["session_log"])[0]["frozen"] == spec
    # One limits preflight plus one catalog-version/source-ID freeze.
    assert client.schema_calls == 2
    assert client.set_background_calls == []


@pytest.mark.parametrize("spec, needle", [
    ({"enabled": True, "preset": "realistic"}, "unknown field"),
    ({"catalog_version": 2, "enabled": True, "sources": {}, "scale": 2.0}, "unknown field"),
    ({"catalog_version": 2, "enabled": True}, "missing required field"),
    (_bg_spec(sources={"nope": {"enabled": True, "scale": 1.0}}), "unknown background source"),
    (_bg_spec(sources={"sample_diffuse": {"enabled": True, "scale": 1.0}}),
     "unknown background source"),
    (_bg_spec(sources={"environment_flat": {"enabled": True}}), "missing required field"),
    (_bg_spec(sources={"environment_flat": {"enabled": "true", "scale": 1.0}}),
     "'enabled' must be a boolean"),
    (_bg_spec(catalog_version=1), "catalog version mismatch"),
])
def test_an_unusable_sample_background_aborts_the_campaign_before_any_scan(tmp_path, spec, needle):
    """An honest setup abort: nothing is submitted and nothing is silently defaulted."""
    omega = lambda h: (h - 2.0) * 2.0
    client = _BackgroundTavi(omega)
    with pytest.raises(ValueError) as excinfo:
        _run(tmp_path, _objective(), client, make_mock_runner(omega), sample_background=spec)
    assert needle in str(excinfo.value)
    assert client.jobs == {} and client.validate_bodies == []


def test_campaign_refuses_a_v1_instrument_background_schema_before_any_scan(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = _BackgroundTavi(omega, background_schema="tavi.background/1")
    with pytest.raises(ValueError, match=r"tavi\.background/2 is required"):
        _run(tmp_path, _objective(), client, make_mock_runner(omega),
             sample_background=_bg_spec(enabled=False))
    assert client.jobs == {} and client.validate_bodies == []


def test_the_instrument_config_sentinel_stamps_nothing(tmp_path):
    """The one deliberate opt-out: inherit TAVI's live mutable configuration, freeze nothing."""
    omega = lambda h: (h - 2.0) * 2.0
    client = _BackgroundTavi(omega)
    summary, _ws = _run(tmp_path, _objective(), client, make_mock_runner(omega),
                        sample_background=loop_mod.INSTRUMENT_CONFIG_BACKGROUND)

    assert client.jobs
    assert all("background" not in body for body in client.jobs.values())
    assert all("background" not in body for body in client.validate_bodies)
    assert _frozen_events(summary["session_log"]) == []
    assert client.schema_calls == 1     # the limits preflight only; no background resolution
    assert client.set_background_calls == []


def test_an_undeclared_campaign_stamps_a_frozen_disabled_background(tmp_path):
    """Omission is a declaration of CLEAN, not an invitation to inherit the instrument's profile.
    Sending no key let an enabled persisted TAVI profile silently consume a live campaign's budget."""
    omega = lambda h: (h - 2.0) * 2.0
    client = _BackgroundTavi(omega)
    summary, _ws = _run(tmp_path, _objective(), client, make_mock_runner(omega))

    expected = _normalized_bg(enabled=False)
    assert client.jobs and all(body["background"] == expected for body in client.jobs.values())
    assert client.validate_bodies and all(body["background"] == expected
                                          for body in client.validate_bodies)
    frozen = _frozen_events(summary["session_log"])
    assert len(frozen) == 1 and frozen[0]["frozen"] == expected
    # The log distinguishes declared-clean from defaulted-clean.
    assert frozen[0]["defaulted"] is True and frozen[0]["spec"] is None
    # One limits preflight plus one catalog freeze, including the current version.
    assert client.schema_calls == 2
    assert client.set_background_calls == []


def test_a_declared_clean_campaign_is_not_marked_defaulted(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = _BackgroundTavi(omega)
    summary, _ws = _run(tmp_path, _objective(), client, make_mock_runner(omega),
                        sample_background=_bg_spec(enabled=False))
    assert "defaulted" not in _frozen_events(summary["session_log"])[0]


def test_dropping_a_declared_background_on_resume_aborts_before_any_spend(tmp_path):
    """The resume bypass the old omission semantics left open: a campaign that simply forgot its
    background used to sail past the drift guard and be caught only downstream, after the spend."""
    omega = lambda h: (h - 2.0) * 2.0
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    cfg = {"workspace": ws, "client": _BackgroundTavi(omega), "run_isar": make_mock_runner(omega),
           "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8}, "seed": 7,
           "sample_background": _bg_spec(sources={
               "environment_flat": {"enabled": True, "scale": 1.0}})}
    run_campaign(_objective(), cfg)

    forgetful = _BackgroundTavi(omega)
    resumed = dict(cfg, client=forgetful)
    resumed.pop("sample_background")
    with pytest.raises(ValueError) as excinfo:
        run_campaign(_objective(), resumed)
    assert "background_fingerprint_drift" in str(excinfo.value)
    assert forgetful.jobs == {} and forgetful.validate_bodies == []


def test_resume_refuses_when_the_background_catalog_version_drifted(tmp_path):
    """A catalog-v1 workspace refuses a catalog-v2 resume before spending anything."""
    omega = lambda h: (h - 2.0) * 2.0
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    spec = _bg_spec(
        catalog_version=1,
        sources={"environment_flat": {"enabled": True, "scale": 1.0}})
    cfg = {"workspace": ws,
           "client": _BackgroundTavi(
               omega, catalog_version=1, source_ids=_CATALOG_V1_SOURCE_IDS),
           "run_isar": make_mock_runner(omega),
           "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8}, "seed": 7,
           "sample_background": dict(spec)}
    run_campaign(_objective(), cfg)

    drifted = _BackgroundTavi(omega)
    with pytest.raises(ValueError) as excinfo:
        run_campaign(_objective(), dict(cfg, client=drifted, sample_background=dict(spec)))
    assert "background_fingerprint_drift" in str(excinfo.value)
    assert "start a new campaign workspace" in str(excinfo.value)
    assert drifted.jobs == {}


def test_a_resume_under_the_same_background_is_allowed(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    cfg = {"workspace": ws, "client": _BackgroundTavi(omega), "run_isar": make_mock_runner(omega),
           "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8}, "seed": 7,
           "sample_background": _bg_spec(sources={
               "environment_flat": {"enabled": True, "scale": 1.0}})}
    summary1 = run_campaign(_objective(), cfg)
    summary2 = run_campaign(_objective(), dict(cfg, client=_BackgroundTavi(omega)))

    assert summary2["stop"]["state"] in {"met", "incomplete", "infeasible"}
    assert len(_frozen_events(summary1["session_log"])) == 2, "one frozen record per invocation"


def test_resume_refuses_a_v1_frozen_workspace_with_a_clear_schema_error(tmp_path):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    cfg = _campaign_config(_objective(), {
        "workspace": ws, "client": _BackgroundTavi(), "run_isar": lambda *a, **k: None,
        "calibration": _UNITY_CAL, "sample_background": _bg_spec(enabled=False),
    })
    log_event(cfg.log_path, "decide", {
        "event": "sample_background_frozen",
        "frozen": {"enabled": False, "terms": []},
    })

    with pytest.raises(ValueError, match=r"frozen under tavi\.background/1.*new campaign workspace"):
        loop_mod._freeze_sample_background(cfg)


def test_the_frozen_background_shares_nothing_with_the_spec(tmp_path):
    """Freezing means a later caller mutation cannot alter the profile already stamped."""
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    client = _BackgroundTavi()
    spec = _bg_spec(sources={
        "sample_elastic_tail": {"enabled": True, "scale": 3.5}})
    cfg = _campaign_config(_objective(), {"workspace": ws, "client": client,
                                          "run_isar": lambda *a, **k: None,
                                          "calibration": _UNITY_CAL, "sample_background": spec})
    frozen, _meta = loop_mod._freeze_sample_background(cfg)

    spec["sources"]["sample_elastic_tail"]["scale"] = 99.0
    assert frozen["sources"]["sample_elastic_tail"]["scale"] == 3.5


def test_scan_and_validate_bodies_omit_background_when_none():
    proposal = _scan_proposal(0.0, 1.0, 0.1)
    assert "background" not in _validate_body(proposal, sample="Al_phonon_DFT")
    assert "background" not in wire._scan_body(proposal, "deterministic", 3,
                                                   sample="Al_phonon_DFT")
    frozen = _normalized_bg(enabled=False)
    body = wire._scan_body(proposal, "deterministic", 3, sample="Al_phonon_DFT",
                               background=frozen)
    assert body["background"] == frozen and body["engine"] == "deterministic"


class _AlwaysFailingTavi(MockTavi):
    """Every submitted job comes back ``failed`` — the instrument never produces data."""

    def wait_scan(self, job_id, timeout=0.0, **kw):
        return {"job_id": job_id, "state": "failed", "error": "detector offline"}


def test_a_persistently_failing_instrument_terminates_instead_of_spinning(tmp_path, monkeypatch):
    """An unlimited budget plus a rejection class that suppresses nothing had NO termination bound.

    ``scan_not_done:*`` is not in the rejection rules, so it classes transient with scope "none" and
    suppresses nothing; the identical action is re-selected and re-submitted every iteration. Nothing
    is ever charged either — with ``budget=None`` the deterministic tier keeps "unlimited", so
    ``spent`` stays 0.0 and the budget exception can never fire. The absolute iteration ceiling is the
    only thing that ends this, and it must end it with a named reason.
    """
    _no_subprocess(monkeypatch)
    monkeypatch.setattr(loop_mod, "_ABSOLUTE_ITERATION_CEILING", 3)
    omega = lambda h: (h - 2.0) * 2.0
    objective = ObjectiveSpec.along_h(
        h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=0.5, budget=None, name="unbounded")

    summary, _ws = _run(tmp_path, objective, _AlwaysFailingTavi(omega), make_mock_runner(omega))

    assert summary["stop"]["state"] == "incomplete"
    reason = summary["stop"]["reasons"][0]
    assert "absolute iteration ceiling" in reason and "3" in reason
    assert summary["stop"]["terminal_cause"] == loop_mod.TERMINAL_CONTROLLER_FAULT

    events = read_log(summary["session_log"])
    ceilings = [event["payload"] for event in events
                if event["kind"] == "exception"
                and event["payload"].get("action") == "iteration_ceiling_reached"]
    assert len(ceilings) == 1 and ceilings[0]["ceiling"] == 3
    stops = [event for event in events if event["kind"] == "stop"]
    assert len(stops) == 1 and stops[0]["payload"]["resume_supported"] is True
    # Certification must not run on a controller fault.
    assert not any((event.get("payload") or {}).get("event") == "certify_start"
                   for event in events if event["kind"] == "decide")


def test_acquisition_provenance_names_the_authority_decision_and_executed_identity(tmp_path):
    """In authoritative mode the record must say which of two selections it means.

    ``ScanDesignRealizedComparison.selected_request_fingerprint`` always returns the LEGACY request,
    while ``apply_selected_request`` overwrites ``scan_design_realized_selected_request_fingerprint``
    with the request the authority decision actually granted (the target core). The persisted record
    previously carried the overwritten value with nothing naming the decision that caused it, so an
    auditor loading the comparison beside the record and applying its own accessor got a different
    answer with no way to tell which was right.
    """
    from isar.drive.loop import _persist_acquisition_provenance
    from isar.schema.card import Card

    shutil.copy(Path(loop_mod.__file__).resolve().parents[1] / "parsers" / "cards"
                / "tavi.policy.toml", tmp_path / "card.policy.toml")
    decision_ref = {"kind": "scan_design_authority_decision", "sha256": "a" * 64,
                    "path": f"report/scan-design/{'a' * 64}.json"}
    _persist_acquisition_provenance(tmp_path, {
        "job_id": "j1", "acquisition_id": "acq-1", "acquisition_target_energy": 5.0,
        "submitted_ncount": 10_000, "kind": "production",
        # What the loop executed, after apply_selected_request moved it to the target core.
        "scan_design_realized_selected_request_fingerprint": "t" * 64,
        "scan_design_realized_selected_grid_fingerprint": "g" * 64,
        "scan_design_authority_decision_ref": decision_ref,
        "scan_design_authority_decision_fingerprint": "a" * 64,
        "scan_design_authority_selected_request_fingerprint": "t" * 64,
        "scan_design_authority_selected_grid_fingerprint": "g" * 64,
    })

    entry = Card.load(tmp_path / "card.policy.toml",
                      tmp_path / "card.state.json").state.acquisition_provenance[-1]
    assert entry["scan_design_authority_decision_ref"] == decision_ref
    assert entry["scan_design_authority_decision_fingerprint"] == "a" * 64
    assert entry["scan_design_authority_selected_request_fingerprint"] == "t" * 64
    assert entry["scan_design_authority_selected_grid_fingerprint"] == "g" * 64
    # Additive: the pre-existing key keeps its meaning and value.
    assert entry["scan_design_realized_selected_request_fingerprint"] == "t" * 64


def test_a_shadow_only_acquisition_record_carries_no_authority_keys(tmp_path):
    """Shadow/off mode records the authority keys as ``None`` — presence is what marks a decision."""
    from isar.drive.loop import _persist_acquisition_provenance
    from isar.schema.card import Card

    shutil.copy(Path(loop_mod.__file__).resolve().parents[1] / "parsers" / "cards"
                / "tavi.policy.toml", tmp_path / "card.policy.toml")
    _persist_acquisition_provenance(tmp_path, {
        "job_id": "j1", "acquisition_id": "acq-1", "acquisition_target_energy": 5.0,
        "submitted_ncount": 10_000, "kind": "production",
        "scan_design_realized_selected_request_fingerprint": "l" * 64,
    })

    entry = Card.load(tmp_path / "card.policy.toml",
                      tmp_path / "card.state.json").state.acquisition_provenance[-1]
    assert entry["scan_design_authority_decision_ref"] is None
    assert entry["scan_design_authority_selected_request_fingerprint"] is None


def test_the_campaign_provenance_block_travels_on_the_acquisition_record(tmp_path):
    """SCENE-4 PR 4.3 (D2): the request artifact carries the four campaign-provenance facts to the
    delivery pipeline; a request declaring none records all four as None."""
    from isar.drive.loop import _persist_acquisition_provenance
    from isar.schema.card import Card

    policy = (Path(loop_mod.__file__).resolve().parents[1] / "parsers" / "cards"
              / "tavi.policy.toml")
    shutil.copy(policy, tmp_path / "card.policy.toml")
    base = {"acquisition_target_energy": 5.0, "submitted_ncount": 10_000, "kind": "production"}
    _persist_acquisition_provenance(tmp_path, {
        **base, "job_id": "j1", "acquisition_id": "acq-1",
        "topology_role": "context_probe", "program_fingerprint": "pf",
        "program_step": "step-1", "path_fingerprint": "pathfp"})
    _persist_acquisition_provenance(tmp_path, {**base, "job_id": "j2",
                                               "acquisition_id": "acq-2"})

    state = Card.load(tmp_path / "card.policy.toml", tmp_path / "card.state.json").state
    driven, manual = state.acquisition_provenance[-2], state.acquisition_provenance[-1]
    assert [driven[k] for k in ("topology_role", "program_fingerprint", "program_step",
                                "path_fingerprint")] == ["context_probe", "pf", "step-1", "pathfp"]
    assert [manual[k] for k in ("topology_role", "program_fingerprint", "program_step",
                                "path_fingerprint")] == [None, None, None, None]


def test_a_malformed_topology_role_refuses_at_submission_not_at_the_stub(tmp_path):
    """C1 review finding 2: validation lives where refusing costs one request. A bad role surviving
    to the pre-dispatch stub would abort the whole delivery batch and destroy the attempt-proof
    record that exists to survive failure — so the driver refuses to persist the submission."""
    from isar.drive.loop import _persist_acquisition_provenance
    from isar.schema.card import Card

    policy = (Path(loop_mod.__file__).resolve().parents[1] / "parsers" / "cards"
              / "tavi.policy.toml")
    shutil.copy(policy, tmp_path / "card.policy.toml")
    with pytest.raises(ValueError, match="topology_role"):
        _persist_acquisition_provenance(tmp_path, {
            "acquisition_target_energy": 5.0, "submitted_ncount": 10_000, "kind": "production",
            "job_id": "j-bad", "acquisition_id": "acq-bad", "topology_role": "diagnostic"})
    state = Card.load(tmp_path / "card.policy.toml", tmp_path / "card.state.json").state
    assert not any(item.get("job_id") == "j-bad"
                   for item in (state.acquisition_provenance or []))


def test_a_canonical_path_request_without_a_path_fingerprint_refuses_at_submission(tmp_path):
    """Same blast radius as the malformed role: an arena declaring the canonical path but naming
    no path is unplaceable downstream (isar.scene.model.partition_key), so it never ships. A
    context_probe declaring no path is legitimate and still persists."""
    from isar.drive.loop import _persist_acquisition_provenance
    from isar.schema.card import Card

    policy = (Path(loop_mod.__file__).resolve().parents[1] / "parsers" / "cards"
              / "tavi.policy.toml")
    shutil.copy(policy, tmp_path / "card.policy.toml")
    base = {"acquisition_target_energy": 5.0, "submitted_ncount": 10_000, "kind": "production"}
    with pytest.raises(ValueError, match="canonical_path.*path_fingerprint"):
        _persist_acquisition_provenance(tmp_path, {
            **base, "job_id": "j-nopath", "acquisition_id": "acq-nopath",
            "topology_role": "canonical_path"})
    _persist_acquisition_provenance(tmp_path, {
        **base, "job_id": "j-probe", "acquisition_id": "acq-probe",
        "topology_role": "context_probe"})
    entries = Card.load(tmp_path / "card.policy.toml",
                        tmp_path / "card.state.json").state.acquisition_provenance or []
    assert [e.get("job_id") for e in entries] == ["j-probe"]


# ------------------------------------------------------------- PR #9 R24: the pre-submission gate
def test_analysis_overrun_during_planning_stops_before_any_measurement_is_submitted(
        tmp_path, monkeypatch):
    """The declared analysis budget must be re-checked between planning and submit.

    Planning is itself charged (predictive + shadow + realized scoring) and ``analysis_operation``
    only raises when a NEW operation starts over the limit, so an overrun that happens *inside* a
    started operation reaches the submit boundary silently. With only the pre-planning check, a
    campaign that is under the limit at the top of the iteration submits a full batch of scans and
    their full-tier fits past the budget it declared.
    """
    _no_subprocess(monkeypatch)
    omega = lambda h: (h - 2.0) * 2.0
    burned = {"yes": False}
    real_clock = loop_mod.analysis_clock_state
    real_plan = loop_mod.plan_next

    def burning_plan(*args, **kwargs):
        plan = real_plan(*args, **kwargs)
        burned["yes"] = True          # planning consumed the remaining allowance
        return plan

    def clock(log_path, *args, **kwargs):
        state = dict(real_clock(log_path, *args, **kwargs))
        if burned["yes"]:
            state["spent_seconds"] = 9_999.0
        return state

    monkeypatch.setattr(loop_mod, "plan_next", burning_plan)
    monkeypatch.setattr(loop_mod, "analysis_clock_state", clock)

    summary, _ws = _run(tmp_path, _objective(), MockTavi(omega),
                        make_mock_runner(omega, hesse_only=True))

    events = read_log(summary["session_log"])
    # The load-bearing claim: no measurement was submitted past the declared analysis budget.
    assert not any(event["kind"] == "run" for event in events)
    assert summary["stop"]["state"] == "incomplete"
    assert any("before measurement submission" in reason
               for reason in summary["stop"]["reasons"]), summary["stop"]["reasons"]
    assert [event for event in events if event["kind"] == "stop"]


# ------------------------------------------- PR #9 R27: an invariant violation is a named terminal


def test_the_intensity_map_is_integrated_from_the_counts(tmp_path):
    """The map the forward model prices designs with is read from the counts, not the fitted
    amplitudes, and it reproduces the counts it was read from.

    The fitted amplitudes do not carry the convention the planner needs — the second live run
    published ``phonon.A = 6.7e-5`` against a 5e6 exposure for a scan holding an 18-count peak, five
    orders of magnitude too dim — so every design's expected information was exactly zero.
    """
    from isar.drive.acquisition.eig_planner import branch_counts
    from isar.drive.loop import _planner_scale

    ncount, temperature, fwhm = 5_000_000, 200.0, 0.5
    energy, step, lo = 0.9, 0.125, -0.4
    x = [round(lo + step * i, 6) for i in range(31)]
    # A 7-count background with a Stokes lobe on it; no anti-Stokes in view.
    counts = [7.0] * len(x)
    for i, value in enumerate(x):
        if abs(value - energy) <= 0.3:
            counts[i] += 20.0

    ingest = tmp_path / "data" / "ingest"
    ingest.mkdir(parents=True)
    (ingest / "unit.json").write_text(json.dumps({
        "schema": "isar.unit_data/1", "scan_number": 7, "detector_id": "det",
        "x": x, "counts": counts}), encoding="utf-8")
    log_path = tmp_path / "log.jsonl"
    log_event(log_path, "physical_submission", {
        "acquisition_id": "acq-7", "hkl": [2.1, 0.0, 0.0], "submitted_ncount": ncount})
    cfg = types.SimpleNamespace(data_dir=tmp_path / "data", log_path=log_path)
    observations = [{"scan": 7, "detector": "det", "acquisition_id": "acq-7", "resolved": True,
                     "branch_id": "acoustic", "disposition": "resolved",
                     "branch_candidates": [{"energy_meV": energy, "sigma_meV": 0.02}]}]

    path = ((2.1, 0.0, 0.0),)
    scale = _planner_scale(cfg, observations, path, ("acoustic", "optic"), temperature, (fwhm,))

    # The optic was never measured, so it keeps the planner's class default; the acoustic does not.
    assert scale.peak_counts_meV["acoustic"] != pytest.approx(
        scale.peak_counts_meV["optic"], rel=1e-6)
    assert scale.background_per_point == pytest.approx(7.0 / ncount, rel=1e-9)

    # The contract that matters: feed the map back through the planner's own forward model and it
    # reproduces the counts it was integrated from.
    observed = sum(value - 7.0 for value in counts)
    predicted = branch_counts([energy, 6.0], e_lo=x[0], e_step=step, n_points=len(x),
                              fwhm_meV=fwhm, temperature_K=temperature, scale=scale,
                              branches=("acoustic", "optic"), ncount=ncount)
    assert predicted[0] == pytest.approx(observed, rel=0.25)


def _counts_scan(tmp_path, x, counts, *, ncount, energies, scan=7):
    """One ingested scan plus its submission, the two things the intensity map is read from."""
    ingest = tmp_path / "data" / "ingest"
    ingest.mkdir(parents=True, exist_ok=True)
    (ingest / f"unit-{scan}.json").write_text(json.dumps({
        "schema": "isar.unit_data/1", "scan_number": scan, "detector_id": "det",
        "x": x, "counts": counts}), encoding="utf-8")
    log_event(tmp_path / "log.jsonl", "physical_submission", {
        "acquisition_id": f"acq-{scan}", "hkl": [2.1, 0.0, 0.0], "submitted_ncount": ncount})
    return {"scan": scan, "detector": "det", "acquisition_id": f"acq-{scan}", "resolved": True,
            "branch_id": "acoustic", "disposition": "resolved",
            "branch_candidates": [{"energy_meV": e, "sigma_meV": 0.02} for e in energies]}


def test_the_elastic_term_is_read_only_where_a_window_sees_it(tmp_path):
    """A window with a mode sitting on zero has not measured the elastic line, and must say nothing
    about it rather than hand the mode's own counts to the elastic term.

    Measured on the live path: at the reduced-q origin the acoustic Stokes/anti-Stokes pair straddles
    zero inside one resolution width, and integrating it as elastic read ~30 counts*meV per 1e7 there
    against ~0 at every separated point — a drifting elastic term that was never a measurement.
    """
    from isar.drive.loop import _planner_scale

    ncount, temperature, fwhm = 5_000_000, 200.0, 0.5
    step, lo = 0.12, -1.2
    x = [round(lo + step * i, 6) for i in range(21)]

    # (a) a 0.5 meV mode straddling zero: nothing here measures the elastic.
    straddling = [2.0] * len(x)
    for i, value in enumerate(x):
        if min(abs(value - 0.5), abs(value + 0.5)) <= 0.25:
            straddling[i] += 15.0
    # (b) a mode far from zero, and a genuinely empty elastic position.
    separated = [2.0] * len(x)
    for i, value in enumerate(x):
        if abs(value - 1.8) <= 0.25:
            separated[i] += 15.0

    cfg = types.SimpleNamespace(data_dir=tmp_path / "data", log_path=tmp_path / "log.jsonl")
    path = ((2.1, 0.0, 0.0),)
    obs_a = _counts_scan(tmp_path, x, straddling, ncount=ncount, energies=[0.5], scan=7)
    obs_b = _counts_scan(tmp_path, x, separated, ncount=ncount, energies=[1.8], scan=8)

    only_straddling = _planner_scale(cfg, [obs_a], path, ("acoustic", "optic"), temperature, (fwhm,))
    both = _planner_scale(cfg, [obs_a, obs_b], path, ("acoustic", "optic"), temperature, (fwhm,))

    # The straddling scan says nothing about the elastic, so the class default stands.
    default = default_intensity_scale(("acoustic", "optic"))
    assert only_straddling.elastic_counts_meV == pytest.approx(default.elastic_counts_meV)
    # The separated scan looked at zero and found nothing there: that IS the measurement.
    assert both.elastic_counts_meV == pytest.approx(0.0)


def test_a_planner_stop_that_is_not_the_prize_is_incomplete(tmp_path):
    """``no_admissible_window`` means the planner ran out of moves, not that the map is at the
    requested precision. Reporting it ``met`` would certify a campaign that measured nothing.

    A point ceiling of three drops every candidate window at a deficit point, which is exactly the
    state that stop names.
    """
    omega = lambda h: (h - 2.0) * 2.0
    objective = _objective()
    objective.steering = {**objective.steering, "max_scan_points": 3}
    summary, _workspace = _run(tmp_path, objective, MockTavi(omega), make_mock_runner(omega))

    assert summary["stop"]["state"] == "incomplete"
    assert "without the prize" in summary["stop"]["reasons"][0]
    assert "no_admissible_window" in summary["stop"]["reasons"][0]


def test_only_a_certifying_interval_is_a_candidates_precision():
    """``diagnostic_halfwidth`` is the same calibrated scale but a different claim: a row publishes
    it INSTEAD of a certifying interval when its model is not settled. Reading it as the candidate's
    sigma let the prize be claimed on a point certification then refused to certify — invisible,
    because the two numbers are equal on every row where it does not matter.
    """
    from isar.drive.loop import _planner_candidates

    path = ((2.1, 0.0, 0.0),)
    domain = (-2.0, 12.0)
    unknown = 0.5 * (domain[1] - max(domain[0], 0.0))
    candidates = [{"energy_meV": 1.0, "sigma_meV": 0.0003}]

    certifying = {"hkl": (2.1, 0.0, 0.0), "certifiable": True, "halfwidth": 0.02,
                  "diagnostic_halfwidth": 0.02, "modality": "fold", "pool_consistent": True,
                  "branch_candidates": candidates}
    assert _planner_candidates([certifying], path, domain)[0][0].sigma_meV == pytest.approx(0.02)

    # Not model-robust: it still publishes a diagnostic interval, and that is NOT a precision.
    for spoiled in ({"certifiable": False, "halfwidth": None},
                    {"modality": "fork"},
                    {"pool_consistent": False},
                    {"halfwidth": None}):
        row = {**certifying, **spoiled}
        sigma = _planner_candidates([row], path, domain)[0][0].sigma_meV
        assert sigma == pytest.approx(unknown), spoiled
        assert sigma != pytest.approx(0.02), spoiled


def test_a_non_model_robust_certification_cannot_produce_met(tmp_path):
    """The consequence of the rule above, end to end: if the full-tier pass leaves every row without
    a certifying interval, the prize is never claimable and the campaign keeps buying counts."""
    omega = lambda h: (h - 2.0) * 2.0
    objective = _objective()
    # A budget, because the point of the test is that this campaign never finishes: without one it
    # would keep buying counts at points certification will not certify until the iteration ceiling.
    objective.budget = {"kind": "neutrons", "amount": 4.0e8}
    summary, _workspace = _run(tmp_path, objective, MockTavi(omega),
                               make_mock_runner(omega, model_robust=False))

    assert summary["stop"]["state"] == "incomplete"
    assert "budget" in " ".join(summary["stop"]["reasons"])
    assert not any(point.get("converged") for point in summary["grid"])


def test_delivery_evidence_is_a_probability_not_a_set():
    """A mode at P(optic)=0.55 half-argues the optic was delivered. Rounding that to "seen" threw
    away exactly the doubt the identity gate exists to price."""
    from isar.drive.loop import _candidate_probabilities

    branches = ("acoustic", "optic")
    pooled = {"probabilities": {"acoustic": 0.45, "optic": 0.55, "new": 0.0}}
    assert _candidate_probabilities(pooled, {}, branches) == pytest.approx(
        {"acoustic": 0.45, "optic": 0.55})

    # A per-scan row carries no probabilities; its tracker label is a probability of one.
    assert _candidate_probabilities({}, {"branch_id": "acoustic"}, branches) == pytest.approx(
        {"acoustic": 1.0, "optic": 0.0})

    # A mode nothing labels asserts nothing about any branch.
    assert _candidate_probabilities({}, {}, branches) == {}


def test_abstained_points_name_only_underpowered_masters():
    """The fitter's ``underpowered`` abstain is "could not see", not "saw nothing"; the planner is
    told which grid points that is true of so their silence is not read as a null."""
    from isar.drive.loop import _abstained_points
    path = [(2.3, 0.0, 0.0), (2.35, 0.0, 0.0), (2.4, 0.0, 0.0)]
    rows = [{"hkl": (2.3, 0.0, 0.0), "disposition": "abstain", "verdict": "underpowered"},
            {"hkl": (2.35, 0.0, 0.0), "disposition": "declined", "verdict": "no_signal"},
            {"hkl": (2.4, 0.0, 0.0), "disposition": "resolved", "verdict": "resolved"}]
    assert _abstained_points(rows, path) == {0: True}
