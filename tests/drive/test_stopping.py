"""The stopping criterion: master (pooled) convergence + unmeetable branches."""
import pytest

from isar.drive.acquisition.calibration import Calibration
from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot
from isar.drive.supervisor import stopping as st

_BUDGET = {"kind": "neutrons", "amount": 1e12, "spent": 0.0}


def _obj(tol=0.5):
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=tol,
                                 budget={"kind": "neutrons", "amount": 1e12})


def _obs(hkl, kind, hw, resolved=True, disp="resolved", certifiable=True, uncertifiable_kind=None):
    return {"hkl": hkl, "kind": kind, "calibrated_halfwidth": hw, "resolved": resolved,
            "disposition": disp, "omega0": 3.0 if resolved else None,
            "certifiable": certifiable, "uncertifiable_kind": uncertifiable_kind}


def _master(hkl, hw, disp="resolved", reliable=True, n=2, members=None, certifiable=None):
    # A pooled-fit convergence row as loop._master_obs_from_row builds it. `certifiable` defaults to
    # `reliable` (the pre-S3 equivalence); pass it explicitly to model a γ-at-bound master whose
    # profile fallback makes it `reliable` but whose flag makes it uncertifiable.
    if certifiable is None:
        certifiable = reliable
    return {"hkl": hkl, "omega0": 3.0, "halfwidth": hw, "reliable": reliable,
            "certifiable": certifiable,
            "usable": disp in st.USABLE_DISPOSITIONS and hw is not None,
            "disposition": disp, "n_members": n,
            "members": (members if members is not None else [str(i) for i in range(n)])}


# --------------------------------------------------------------------------- master provenance
def test_per_q_carries_master_members():
    # evaluate_stop's per_q rollup surfaces the pooled master's member labels (chain of custody).
    obj = _obj()
    masters = [_master((h, 0.0, 0.0), 0.30, n=3, members=["473", "493", "494"])
               for h in (2.1, 2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, [], masters)
    decision = st.evaluate_stop(obj, grid, _BUDGET)
    per_q = decision.to_dict()["per_q"]
    assert all(q["master_members"] == ["473", "493", "494"] for q in per_q)


def test_summarize_grid_skips_unlocated_rows_and_does_not_mutate_master_input():
    obj = _obj()
    master = _master((2.1, 0.0, 0.0), 0.3, members=["a", "b"])

    grid = st.summarize_grid(obj, [{"kind": "production", "hkl": None}], [master, {"hkl": None}])

    assert grid[0].master["production_batch_fingerprint"]
    assert "production_batch_fingerprint" not in master
    assert all(point.n_production == 0 for point in grid)


def test_interval_floor_window_is_clamped_to_one():
    obj = _obj()
    obj.steering["interval_floor_window"] = 0

    assert all(point.interval_floor_window == 1 for point in st.summarize_grid(obj, []))


# --------------------------------------------------------------------------- master convergence
def test_every_master_within_tolerance_converges_without_a_met_verdict():
    """``met`` belongs to the EIG planner's prize; ``evaluate_stop`` only rolls the grid up."""
    obj = _obj()
    masters = [_master((h, 0.0, 0.0), 0.30) for h in (2.1, 2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, [], masters)
    assert all(point.converged for point in grid)
    assert st.evaluate_stop(obj, grid, _BUDGET).state == "continue"


def test_inferred_unresolved_gamma_is_excluded_from_precision_completion():
    obj = _obj()
    origin = st.GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5,
                      track_state="inferred_unresolved",
                      structural_endpoint_status="inferred_unresolved",
                      is_reduced_q_origin=True,
                      evidence_master={"track_state": "inferred_unresolved",
                                       "branch_candidates": [{"energy_meV": 5.8}]})
    measured = st.GridQ(index=1, hkl=(2.05, 0.0, 0.0), q=0.05, tolerance=0.5,
                        master=_master((2.05, 0.0, 0.0), 0.3))

    decision = st.evaluate_stop(obj, [origin, measured], _BUDGET)

    assert decision.per_q[0]["assessment_state"] == "inferred_unresolved"
    assert decision.per_q[0]["precision_coverage_eligible"] is False
    assert decision.per_q[0]["omega0"] is None
    assert measured.converged and not origin.precision_coverage_eligible


def test_bare_inferred_state_without_structural_origin_cannot_shrink_denominator():
    malformed = st.GridQ(index=0, hkl=(2.5, 0.0, 0.0), q=0.5, tolerance=0.5,
                         track_state="inferred_unresolved",
                         evidence_master={"track_state": "inferred_unresolved"})
    measured = st.GridQ(index=1, hkl=(2.55, 0.0, 0.0), q=0.55, tolerance=0.5,
                        master=_master((2.55, 0.0, 0.0), 0.3))

    decision = st.evaluate_stop(_obj(), [malformed, measured], _BUDGET)

    assert decision.state != "met"
    assert decision.per_q[0]["precision_coverage_eligible"] is True


def test_hypothesis_campaign_requires_new_production_pool_between_crossings():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    obj = ObjectiveSpec(path=[(2.0, 0.0, 0.0)], tolerance_meV=0.5,
                        budget={"kind": "neutrons", "amount": 1e12},
                        target_branch="optic", **frozen)
    first = _master((2.0, 0.0, 0.0), 0.3, members=["batch-1"])
    grid = st.summarize_grid(obj, [], [first])
    fingerprint = grid[0].master["production_batch_fingerprint"]
    assert not grid[0].converged
    marker = {"hkl": (2.0, 0.0, 0.0), "kind": "certification_crossing",
              "production_batch_fingerprint": fingerprint}
    # Re-evaluating the unchanged pool cannot produce the second crossing.
    assert not st.summarize_grid(obj, [marker], [first])[0].converged
    second = _master((2.0, 0.0, 0.0), 0.25, members=["batch-1", "batch-2"])
    assert st.summarize_grid(obj, [marker], [second])[0].converged


def test_crossing_history_compares_only_latest_and_preserves_repeated_fingerprints():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    obj = ObjectiveSpec(path=[(2.0, 0.0, 0.0)], tolerance_meV=0.5,
                        budget={"kind": "neutrons", "amount": 1e12},
                        target_branch="optic", **frozen)
    master = _master((2.0, 0.0, 0.0), 0.3, members=["batch-a"])
    fingerprint = st.summarize_grid(obj, [], [master])[0].master["production_batch_fingerprint"]
    markers = [
        {"hkl": (2.0, 0.0, 0.0), "kind": "certification_crossing",
         "production_batch_fingerprint": fingerprint},
        {"hkl": (2.0, 0.0, 0.0), "kind": "certification_crossing",
         "production_batch_fingerprint": "intervening"},
        {"hkl": (2.0, 0.0, 0.0), "kind": "certification_crossing",
         "production_batch_fingerprint": fingerprint},
    ]
    grid = st.summarize_grid(obj, markers, [master])
    assert grid[0].crossing_fingerprints == [fingerprint, "intervening", fingerprint]
    assert not grid[0].converged


def test_flagged_master_with_reliable_interval_converges():
    # The accept-flagged decision: a model_incomplete fit whose omega0 interval is reliable + tight
    # still delivers a usable omega0 for the tolerance objective.
    obj = _obj()
    masters = [_master((h, 0.0, 0.0), 0.30, disp="flagged") for h in (2.1, 2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, [], masters)
    assert all(point.converged for point in grid)


def test_unreliable_master_does_not_converge():
    # A gamma-at-bound collapse with no usable interval (reliable=False) must not certify, even tiny.
    obj = _obj()
    masters = [_master((h, 0.0, 0.0), 0.001, reliable=False) for h in (2.1, 2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, [], masters)
    assert st.evaluate_stop(obj, grid, _BUDGET).state != "met"


def test_pool_inconsistent_master_never_certifies_even_when_tight():
    # AP8 safety property: the identity tracker now keeps a pool_inconsistent master's candidate so the
    # trajectory stays connected, but that must NOT let the master certify. Convergence seats only on a
    # consistent, certifiable interval (_master_hw returns None when pool_consistent is False), so an
    # inconsistent point stays unconverged no matter how tight its interval — and is instead re-acquired.
    obj = _obj()
    masters = []
    for h in (2.1, 2.2, 2.3, 2.4):
        m = _master((h, 0.0, 0.0), 0.001)      # a tiny, otherwise-certifiable interval
        m["pool_consistent"] = False
        masters.append(m)
    grid = st.summarize_grid(obj, [], masters)
    assert all(point.cert_halfwidth is None and not point.converged for point in grid)
    assert st.evaluate_stop(obj, grid, _BUDGET).state != "met"


def test_single_production_cannot_rescue_degraded_master():
    obj = _obj()
    obs = [_obs((h, 0.0, 0.0), "production", 0.3, disp="flagged") for h in (2.1, 2.2, 2.3, 2.4)]
    masters = [_master((h, 0.0, 0.0), 0.001, disp="junk", reliable=False) for h in (2.1, 2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert st.evaluate_stop(obj, grid, _BUDGET).state == "continue"
    assert all(not point.converged and point.best_prod_halfwidth is None for point in grid)


def test_driver_inserted_observation_cannot_occupy_or_penalize_requested_q():
    obj = _obj()
    probe = _obs((2.10000001, 0.0, 0.0), "production", 0.01)
    probe.update({"driver_inserted": True, "certification_eligible": False,
                  "proposal_cost": 1234.0})
    first = st.summarize_grid(obj, [probe])[0]
    assert first.n_production == 0
    assert first.n_decline == 0
    assert first.counts_spent == 0.0
    assert first.best_prod_halfwidth is None
    assert not first.visited and not first.converged


def test_unpooled_track_state_blocks_scalar_convergence_and_model_seeding():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    obj = ObjectiveSpec(path=[(2.0, 0.0, 0.0)], tolerance_meV=0.5,
                        budget={"kind": "neutrons", "amount": 1e12},
                        target_branch="optic", target_selector="tracked_branch", **frozen)
    obs = _obs((2.0, 0.0, 0.0), "production", 0.2)
    obs.update({"branch_id": "optic", "track_state": "ambiguous", "track_revision": 2})
    # The individual ambiguity cannot become authority; the target master still lacks confirmation.
    grid = st.summarize_grid(obj, [obs], [_master((2.0, 0.0, 0.0), 0.1)])
    assert grid[0].track_state is None
    assert not grid[0].converged
    assert st.model_points(grid) == []


def test_malformed_master_numerics_do_not_seed_dispersion_model():
    malformed = _master((2.1, 0.0, 0.0), 0.1)
    malformed["omega0"] = "not-a-number"
    grid = st.summarize_grid(
        _obj(),
        [],
        [malformed],
    )
    assert st.model_points(grid) == []

    grid[0].master["omega0"] = 6.0
    grid[0].master["halfwidth"] = float("inf")
    assert st.model_points(grid) == []


# --------------------------------------------------------------------------- S3 uncertifiable state
def test_unreliable_best_prod_does_not_converge():
    # The mid-zone hole (§5.3): a usable single-production fit with an UNCERTIFIABLE interval
    # (γ-at-bound, interval_reliable=False) must never seat convergence, however tiny its half-width.
    obj = _obj()
    obs = [_obs((h, 0.0, 0.0), "production", 0.001, disp="flagged",
                certifiable=False, uncertifiable_kind="gamma_at_bound") for h in (2.1, 2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs)
    assert all(not g.converged for g in grid)
    assert st.evaluate_stop(obj, grid, _BUDGET).state != "met"
    assert all(g.best_prod_halfwidth is None for g in grid)


def test_flagged_master_with_profile_does_not_converge():
    # The master-level mirror of the hole: a γ-at-bound master whose profile fallback makes it
    # `reliable` but whose flag makes it uncertifiable must not certify.
    obj = _obj()
    masters = [_master((h, 0.0, 0.0), 0.001, disp="flagged", reliable=True, certifiable=False)
               for h in (2.1, 2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, [], masters)
    assert st.evaluate_stop(obj, grid, _BUDGET).state != "met"


def test_scan_uncertifiability_never_becomes_master_state_or_stuck():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", 0.001, disp="flagged",
                certifiable=False, uncertifiable_kind="gamma_at_bound")
           for _ in range(st.MAX_PROD_ATTEMPTS)]
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]   # the other three converge
    grid = st.summarize_grid(obj, obs, masters)
    g0 = grid[0]
    assert g0.n_decline == 0 and g0.n_uncertifiable == 0
    assert not g0.converged
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert d.state == "continue"
    per_q = {tuple(q["hkl"]): q for q in d.to_dict()["per_q"]}
    assert per_q[(2.1, 0.0, 0.0)]["assessment_state"] == "unmeasured"


def test_scan_fit_quality_does_not_update_decline_counters():
    obj = _obj()
    obs = ([_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="junk"),
            _obs((2.1, 0.0, 0.0), "production", 0.001, disp="flagged",
                 certifiable=False, uncertifiable_kind="gamma_at_bound")])
    grid = st.summarize_grid(obj, obs)
    assert grid[0].n_decline == 0 and grid[0].n_uncertifiable == 0


def test_fast_tier_scan_sequence_never_abandons_q():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, disp="resolved",
                certifiable=False, uncertifiable_kind="fast_tier")
           for _ in range(st.MAX_PROD_ATTEMPTS)]
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert d.state == "continue"


def test_scan_validation_verdicts_do_not_establish_physical_infeasibility():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="declined")
           for _ in range(st.MAX_DECLINES)]
    for o in obs:
        o["verdict"] = "validation_rejected"
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert grid[0].n_infeasible == 0 and grid[0].n_decline == 0
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert d.state == "continue"


def test_unmodeled_individual_fits_do_not_abandon_q():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="unmodeled")
           for _ in range(st.MAX_PROD_ATTEMPTS)]
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert grid[0].n_unmodeled == 0
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert d.state == "continue"


def test_mixed_individual_verdicts_remain_acquisition_provenance_only():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="declined"),
           _obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="declined"),
           _obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="junk")]
    obs[0]["verdict"] = obs[1]["verdict"] = "validation_rejected"
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert grid[0].n_infeasible == 0 and grid[0].n_decline == 0


def test_no_usable_individual_fits_still_does_not_abandon_q():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="junk") for _ in range(3)]
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert st.evaluate_stop(obj, grid, _BUDGET).state == "continue"


def test_optic_objective_certifies_only_optic_observations_and_master():
    obj = _obj()
    obj.target_branch = "optic"
    acoustic = _obs((2.1, 0.0, 0.0), "production", 0.1)
    acoustic["branch_id"] = "acoustic"
    optic = _obs((2.2, 0.0, 0.0), "production", 0.1)
    optic["branch_id"] = "optic"
    wrong_master = _master((2.3, 0.0, 0.0), 0.1)
    wrong_master["branch_id"] = "acoustic"
    right_master = _master((2.4, 0.0, 0.0), 0.1)
    right_master["branch_id"] = "optic"
    grid = st.summarize_grid(obj, [acoustic, optic], [wrong_master, right_master])
    assert grid[0].best_prod_halfwidth is None
    assert grid[1].best_prod_halfwidth is None
    assert grid[2].master is None
    assert grid[3].master["hkl"] == right_master["hkl"]
    assert grid[3].master["branch_id"] == "optic"


def test_individual_branch_continuation_does_not_become_terminal_state():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False,
                disp="branch_continuation") for _ in range(st.MAX_PROD_ATTEMPTS)]
    for row in obs:
        row["verdict"] = "branch_continuation"
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    decision = st.evaluate_stop(obj, grid, _BUDGET)
    assert decision.state == "continue"


def test_recenter_rows_share_one_logical_attempt_and_one_decline():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="declined")
           for _ in range(3)]
    for row in obs:
        row.update({"acquisition_id": "A1", "submitted_ncount": 100,
                    "proposal_cost": 1_000.0})
    grid = st.summarize_grid(obj, obs)
    assert grid[0].n_production == 1
    assert grid[0].n_decline == 0
    assert grid[0].counts_spent == pytest.approx(3_000.0)
    assert grid[0].submitted_exposure == pytest.approx(300.0)


def test_logical_exposure_and_last_ncount_do_not_depend_on_representative_row():
    declined = _obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="declined")
    declined.update({"acquisition_id": "A1", "submitted_ncount": 125,
                     "proposal_cost": 1_250.0})
    resolved = _obs((2.1, 0.0, 0.0), "production", 0.4, resolved=True, disp="resolved")
    resolved.update({"acquisition_id": "A1", "submitted_ncount": None,
                     "proposal_cost": 0.0})
    grid = st.summarize_grid(_obj(), [declined, resolved])
    assert grid[0].last_submitted_ncount == 125
    assert grid[0].submitted_exposure == pytest.approx(125.0)


def test_successful_recenter_resolves_its_logical_acquisition():
    obj = _obj()
    missed = _obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="declined")
    found = _obs((2.1, 0.0, 0.0), "production", 0.4, resolved=True, disp="resolved")
    for row in (missed, found):
        row["acquisition_id"] = "A1"
    grid = st.summarize_grid(obj, [missed, found])
    assert grid[0].n_production == 1
    assert grid[0].n_decline == 0
    assert grid[0].best_omega0 is None


def _master_update(exposure, halfwidth, **overrides):
    update = {"kind": "pooled_master_update", "hkl": (2.1, 0.0, 0.0),
              "candidate_id": "acoustic:0.1:lowest_energy", "branch_id": "acoustic",
              "interval_method": "profile", "exposure_bucket": "global",
              "calibration_version": "v1", "effective_exposure": exposure,
              "diagnostic_halfwidth": halfwidth, "certifiable": True}
    update.update(overrides)
    return update


def test_posterior_rejected_candidate_is_a_visible_nonterminal_diagnostic():
    update = _master_update(100.0, 1.0, candidate_state="rejected")
    grid = st.summarize_grid(_obj(), [update])
    assert grid[0].candidate_rejected
    assert "model_ambiguous" in grid[0].diagnostics


def test_wrong_branch_master_updates_cannot_trigger_floor_or_rejection():
    updates = [_master_update(100.0, 1.0, branch_id="optic"),
               _master_update(150.0, 1.0, branch_id="optic"),
               _master_update(220.0, 1.0, branch_id="optic", candidate_state="rejected")]
    grid = st.summarize_grid(_obj(), updates)
    assert grid[0].master_updates == []
    assert not grid[0].candidate_rejected


def test_wrong_branch_master_is_measured_evidence_without_target_delivery():
    wrong = _master((2.1, 0.0, 0.0), 0.2)
    wrong.update({"branch_id": "optic", "branch_candidates": [
        {"energy_meV": 2.7}, {"energy_meV": 6.85}]})
    point = st.summarize_grid(_obj(), [], [wrong])[0]
    assert point.visited and point.evidence_master is not None
    assert point.master is None and point.assessment_state == "ambiguous"
    assert not point.converged


def test_stop_decision_terminal_cause_is_additive_and_omitted_when_absent():
    plain = st.StopDecision("continue", [], [])
    assert plain.terminal_cause is None
    assert "terminal_cause" not in plain.to_dict()
    tagged = st.StopDecision("incomplete", ["frontier_blocked"], [],
                             terminal_cause="action_inventory_exhausted")
    assert tagged.to_dict()["terminal_cause"] == "action_inventory_exhausted"


def test_master_over_tolerance_keeps_accumulating():
    obj = _obj()
    # One master still over tolerance (needs more pooled scans) → continue.
    masters = [_master((2.1, 0.0, 0.0), 0.9)] + [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, [], masters)
    assert st.evaluate_stop(obj, grid, _BUDGET).state == "continue"


def test_continue_when_underdetermined():
    obj = _obj()
    grid = st.summarize_grid(obj, [_obs((2.1, 0.0, 0.0), "quicklook", 0.9)])
    assert st.evaluate_stop(obj, grid, _BUDGET).state == "continue"


# --------------------------------------------------------------------------- unmeetable branches
def test_incomplete_on_budget_exhaustion():
    obj = _obj()
    grid = st.summarize_grid(obj, [_obs((2.1, 0.0, 0.0), "quicklook", 0.9)])
    d = st.evaluate_stop(obj, grid, {"kind": "neutrons", "amount": 100.0, "spent": 100.0})
    assert d.state == "incomplete"
    assert any("budget" in r for r in d.reasons)


def test_infeasible_when_tolerance_below_declared_floor():
    obj = _obj(tol=0.05)
    cal = Calibration.from_dict({"inflation": 2.0, "buckets": [], "center_pull_mean": 0.117})
    grid = st.summarize_grid(obj, [])
    d = st.evaluate_stop(obj, grid, _BUDGET, calibration=cal)
    assert d.state == "infeasible"
    assert all(item["assessment_state"] == "infeasible" for item in d.per_q)
    assert any("floor" in r for r in d.reasons)


def test_declines_are_nonterminal_strategy_inputs():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="junk") for _ in range(3)]
    grid = st.summarize_grid(obj, obs)
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert d.state == "continue"


def test_max_production_count_is_not_a_terminal_cap():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", 0.9) for _ in range(st.MAX_PROD_ATTEMPTS)]
    masters = [_master((2.1, 0.0, 0.0), 0.9)] + [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert d.state == "continue"


def test_unresolved_q_continues_until_global_limit():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="junk") for _ in range(3)]
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]   # the other three converge
    grid = st.summarize_grid(obj, obs, masters)
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert d.state == "continue"


def test_path_scalars_are_arclength():
    obj = _obj()
    s = st.path_scalars(obj.path)
    assert s == pytest.approx([0.0, 0.1, 0.2, 0.3])


def test_match_grid_index_rounds_to_nearest():
    obj = _obj()
    assert st.match_grid_index((2.2000001, 0.0, 0.0), obj.path) == 1
    assert st.match_grid_index((2.9, 0.0, 0.0), obj.path) is None


# --------------------------------------------------------------- acoustic zone-centre exemption
def test_an_acoustic_zone_centre_is_never_a_measurable_target():
    """omega(q->0) -> 0 on an acoustic branch, so the mode sits under the elastic line there.

    The tracker also retires the origin once it can infer the intercept from resolved neighbours,
    but that is earned late and only if the linear fit passes.  Until it does, fitting a DHO at the
    zone centre returns a centre with an error bar thousands of times larger than itself, which
    reads as a confirmed identity rather than an unresolved one — so the campaign never earns the
    exemption and keeps returning.  Three quarters of the scans in the R3 cold-start campaigns went
    to that single q.
    """
    origin = st.GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5,
                      acoustic_target=True, is_reduced_q_origin=True,
                      track_state="confirmed")
    measured = st.GridQ(index=1, hkl=(2.05, 0.0, 0.0), q=0.05, tolerance=0.5,
                        acoustic_target=True, master=_master((2.05, 0.0, 0.0), 0.3))

    decision = st.evaluate_stop(_obj(), [origin, measured], _BUDGET)

    assert origin.precision_coverage_eligible is False
    assert decision.per_q[0]["precision_coverage_eligible"] is False
    assert measured.converged


def test_an_optic_zone_centre_is_still_a_measurable_target():
    """An optic branch has a finite omega at the zone centre and is measurable there.

    The exemption keys on the branch, not on the origin, or an optic campaign would silently stop
    demanding the one q it most wants.
    """
    origin = st.GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5,
                      acoustic_target=False, is_reduced_q_origin=True,
                      track_state="confirmed")

    assert origin.precision_coverage_eligible is True


def test_the_zone_anchor_is_read_from_the_tracker_geometry_not_from_its_inference():
    """``reduced_q_origin_index`` is published from the tracker's first revision, as plain geometry.

    The per-assignment flag only appears once ``branch_tracking`` has inferred the intercept, which
    is exactly the state a stuck campaign never reaches.
    """
    obj = _obj()
    grid = st.summarize_grid(obj, [], [], branch_track={"revision": 1, "reduced_q_origin_index": 0,
                                                        "assignments": []})

    assert grid[0].is_reduced_q_origin is True
    assert grid[0].acoustic_target is True
    assert grid[0].precision_coverage_eligible is False
    assert all(point.precision_coverage_eligible for point in grid[1:])
