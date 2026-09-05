"""PR #37 second review round (2026-09-05): target ownership, catch-up provenance, revision-aware
escalation, the failure counter, and the emitted grid staying inside the energy domain."""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from isar.drive.acquisition.acquire import ScanProposal
from isar.drive.loop import (OPTIC_ELASTIC_EXCLUSION_FWHM, ProposalOutsideEnergyDomain,
                             _anchor_escalation_key, _attempted_anchor_escalations,
                             _clip_to_energy_domain, _consecutive_alerts, _master_obs_from_row,
                             _pending_anchor_escalations, _planner_candidates,
                             _raise_campaign_alert, _row_provenance)
from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot
from isar.drive.session_log import log_event
from isar.drive.acquisition.calibration import Calibration

_UNITY = Calibration.from_dict({"inflation": 1.0, "buckets": [], "center_pull_mean": 0.0})
_PATH = ((2.0, 0.0, 0.0), (2.1, 0.0, 0.0), (2.2, 0.0, 0.0))


# ---------------------------------------------------------------- finding 1: a cleared target owns no width
def _master_row(**overrides) -> dict:
    row = {"scan": 9_000_000, "detector": "det", "h": 2.1, "k": 0.0, "l": 0.0, "pooled": True,
           "status": "fitted", "verdict": "resolved", "disposition": "resolved",
           "model_robust": True, "model_interval_lo": 4.98, "model_interval_med": 5.00,
           "model_interval_hi": 5.02, "omega0": 5.0, "interval_reliable": True,
           "branch_candidates": json.dumps([
               {"candidate_ref": "m:0", "energy_meV": 5.0, "sigma_meV": 0.02,
                "probabilities": {"acoustic": 0.6, "optic": 0.4}},
               {"candidate_ref": "m:1", "energy_meV": 8.0, "sigma_meV": 0.3,
                "probabilities": {"acoustic": 0.4, "optic": 0.6}}]),
           "track_state": "confirmed", "selected_candidate_index": 0, "n_members": 3}
    row.update(overrides)
    return row


def test_a_master_with_a_selected_target_certifies_on_its_model_interval():
    obs = _master_obs_from_row(_master_row(), _UNITY)
    assert obs["certifiable"] and obs["usable"] and obs["omega0"] == pytest.approx(5.0)
    assert obs["halfwidth"] == pytest.approx(0.02)


def test_a_cleared_selection_leaves_no_certified_width_for_the_planner():
    """``_clear_tracked_selection`` nulls omega0 and the selection but not the fit-local model
    interval. That interval describes the fit's primary mode, not the target, so it must not
    restore a certified precision the tracker withdrew — a 0.02 meV width on an ambiguous point
    passed a 0.1 meV bar in the reviewer's counterexample."""
    cleared = _master_row(omega0=None, selected_candidate_index=None, track_state="ambiguous",
                          branch_id=None, interval_reliable=False)
    obs = _master_obs_from_row(cleared, _UNITY)
    assert obs["certifiable"] is False and obs["halfwidth"] is None and obs["omega0"] is None
    assert obs["usable"] is False
    # And the planner sees two detected modes of UNKNOWN precision, never a 0.02 meV candidate.
    obs["branch_candidates"] = json.loads(cleared["branch_candidates"])
    candidates = _planner_candidates([obs], _PATH, (0.0, 20.0))[1]
    assert len(candidates) == 2
    assert all(c.sigma_meV == pytest.approx(10.0) for c in candidates)


# ---------------------------------------------------------------- finding 2: rows keep their own submission
def _proposal(acq: str, ncount: int, h: float) -> ScanProposal:
    return ScanProposal(h=h, k=0.0, l=0.0, e_lo=4.0, e_hi=6.0, e_step=0.1, ncount=ncount,
                        kind="production", reason="test", e_pred=5.0, fwhm_dE=0.8, n_points=21,
                        acquisition_id=acq, meta={"proposal_fingerprint": f"fp-{acq}"})


def test_a_caught_up_row_takes_its_own_submissions_exposure_and_identity():
    """Policy A lets a later analysis publish an earlier scan's row. Submission A (2.10, 20M)
    failed its analysis; submission B (2.20, 80M) succeeded and published both. Each row must be
    folded with its own acquisition, exposure and fingerprint, not B's."""
    current = _proposal("acq-B", 80_000_000, 2.2)
    card_state = {"acquisition_provenance": [
        {"job_id": "j-0001", "acquisition_id": "acq-A", "submitted_ncount": 20_000_000,
         "scan_kind": "production", "proposal_fingerprint": "fp-acq-A",
         "acquisition_action_role": "target", "target_branch_role": "optic"},
        {"job_id": "j-0002", "acquisition_id": "acq-B", "submitted_ncount": 80_000_000,
         "scan_kind": "production", "proposal_fingerprint": "fp-acq-B"}]}
    row_a = {"scan": 1, "detector": "det", "h": 2.1, "acquisition_id": "acq-A"}
    row_b = {"scan": 2, "detector": "det", "h": 2.2, "acquisition_id": "acq-B"}
    a = _row_provenance(row_a, current, card_state)
    b = _row_provenance(row_b, current, card_state)
    assert (a["acquisition_id"], a["ncount"], a["proposal_fingerprint"], a["current"]) == (
        "acq-A", 20_000_000, "fp-acq-A", False)
    assert a["acquisition_action_role"] == "target" and a["target_branch_role"] == "optic"
    assert (b["acquisition_id"], b["ncount"], b["current"]) == ("acq-B", 80_000_000, True)
    # A pre-provenance row (no id) still belongs to the current proposal, as before.
    legacy = _row_provenance({"scan": 3, "detector": "det"}, current, card_state)
    assert legacy["current"] and legacy["acquisition_id"] == "acq-B"


def test_an_unmatched_acquisition_keeps_its_own_id_and_is_logged(tmp_path):
    log_path = tmp_path / "session_log.jsonl"
    prov = _row_provenance({"scan": 9, "detector": "det", "acquisition_id": "acq-ghost"},
                           _proposal("acq-B", 80_000_000, 2.2), {"acquisition_provenance": []},
                           log_path, it=4)
    assert prov["acquisition_id"] == "acq-ghost" and prov["current"] is False
    logged = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert logged[0]["payload"]["action"] == "row_acquisition_unmatched"


# ---------------------------------------------------------------- finding 3: escalation keyed by revision
def _unfolded_rows(revision: str) -> list[dict]:
    return [{"scan_id": "master:q21", "hkl": [2.1, 0.0, 0.0], "h": 2.1, "k": 0.0, "l": 0.0,
             "pooled": True, "disposition": "resolved", "omega0": 5.0, "modality": "unpooled",
             "master_revision": revision}]


def _plan(within_bar: bool = True):
    return SimpleNamespace(per_point=[SimpleNamespace(hkl=(2.1, 0.0, 0.0), exempt=False,
                                                      within_bar=within_bar)])


def test_new_members_reopen_an_anchor_escalation_the_old_revision_used_up(tmp_path, monkeypatch):
    import isar.drive.loop as loop_mod
    rows = _unfolded_rows("rev-1")
    monkeypatch.setattr(loop_mod, "read_results_csv", lambda _ws: rows)
    monkeypatch.setattr(loop_mod, "_slice_rows", lambda r, *a, **k: r)
    key_1 = _anchor_escalation_key(rows, "master:q21")
    assert key_1 == "master:q21@rev-1"
    assert _pending_anchor_escalations(tmp_path, _plan()) == ["master:q21"]
    assert _pending_anchor_escalations(tmp_path, _plan(), attempted={key_1}) == []   # same data: no
    rows[0]["master_revision"] = "rev-2"                                              # new members
    assert _pending_anchor_escalations(tmp_path, _plan(), attempted={key_1}) == ["master:q21"]


def test_attempted_escalations_are_rebuilt_from_the_log_on_resume(tmp_path):
    log_path = tmp_path / "session_log.jsonl"
    log_event(log_path, "escalate_fit", {"phase": "anchor", "iter": 3, "master_ids": ["master:q21"],
                                         "master_keys": ["master:q21@rev-1"]})
    log_event(log_path, "escalate_fit", {"phase": "promote", "iter": 4, "hkl": [2.2, 0, 0]})
    assert _attempted_anchor_escalations(log_path) == {"master:q21@rev-1"}


# ---------------------------------------------------------------- finding 5: only success resets the count
def test_a_failed_iterations_analyze_event_does_not_reset_the_streak(tmp_path):
    """The iteration still emits ``analyze`` after a caught failure; it carries status=failed and
    must not read as a recovery, or a dead worker reports every failure as the first."""
    log_path = tmp_path / "session_log.jsonl"
    seen = []
    for it in range(3):
        seen.append(_raise_campaign_alert(log_path, "analysis_failed", RuntimeError("x"),
                                          it=it)["payload"]["consecutive"])
        log_event(log_path, "analyze", {"iter": it, "n_rows": 0, "status": "failed"})
    assert seen == [1, 2, 3]
    log_event(log_path, "analyze", {"iter": 3, "n_rows": 1, "status": "ok"})
    assert _consecutive_alerts(log_path, "analysis_failed") == 0


# ---------------------------------------------------------------- finding 6: the emitted grid stays in the domain
def _optic_objective(ceiling: float = 20.0) -> ObjectiveSpec:
    frozen = shipped_hypothesis_snapshot("optic", (0.0, ceiling))
    return ObjectiveSpec.along_h(h_start=2.0, h_end=2.2, step=0.1, tolerance_meV=0.1,
                                 budget={"kind": "neutrons", "amount": 1e11},
                                 target_branch="optic", energy_search_meV=(0.0, ceiling), **frozen)


def _cfg(tmp_path, elastic_fwhm: float = 0.3):
    return SimpleNamespace(log_path=tmp_path / "log.jsonl",
                           resolution_cb=lambda h, k, l, dE: {"fwhm_dE": elastic_fwhm if dE == 0.0
                                                              else 0.8})


def test_a_snapped_window_below_the_optic_floor_is_moved_up_along_the_lattice(tmp_path):
    p = _proposal("a", 1e8, 2.1)
    p.e_lo, p.e_hi, p.e_step, p.n_points = 0.8, 4.2, 0.2, 18       # the snap's output for [0.9, 4.1]
    moved = _clip_to_energy_domain(p, _optic_objective(), _cfg(tmp_path), it=0, event_sink=None)
    assert moved
    assert p.e_lo == pytest.approx(1.0) and p.e_lo >= OPTIC_ELASTIC_EXCLUSION_FWHM * 0.3
    assert p.e_hi == pytest.approx(4.2) and p.n_points == 17


def test_a_snapped_window_above_the_ceiling_is_moved_down_along_the_lattice(tmp_path):
    p = _proposal("a", 1e8, 2.1)
    p.e_lo, p.e_hi, p.e_step, p.n_points = 15.96, 20.14, 0.19, 23  # the snap's output for [16, 20]
    assert _clip_to_energy_domain(p, _optic_objective(), _cfg(tmp_path), it=0, event_sink=None)
    assert p.e_hi <= 20.0 and p.e_hi == pytest.approx(19.95)
    assert math.isclose((p.e_hi - p.e_lo) / 0.19, p.n_points - 1, abs_tol=1e-6)


def test_a_window_that_cannot_keep_three_points_inside_the_domain_is_refused(tmp_path):
    p = _proposal("a", 1e8, 2.1)
    p.e_lo, p.e_hi, p.e_step, p.n_points = 0.0, 1.0, 0.2, 6        # entirely inside the exclusion
    with pytest.raises(ProposalOutsideEnergyDomain):
        _clip_to_energy_domain(p, _optic_objective(), _cfg(tmp_path), it=0, event_sink=None)


def test_a_window_already_inside_the_domain_is_untouched(tmp_path):
    p = _proposal("a", 1e8, 2.1)
    p.e_lo, p.e_hi, p.e_step, p.n_points = 4.0, 6.0, 0.1, 21
    assert not _clip_to_energy_domain(p, _optic_objective(), _cfg(tmp_path), it=0, event_sink=None)
    assert (p.e_lo, p.e_hi, p.n_points) == (4.0, 6.0, 21)
