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


# --------------------------------------------------------------------------- master convergence
def test_met_when_all_masters_within_tolerance():
    obj = _obj()
    masters = [_master((h, 0.0, 0.0), 0.30) for h in (2.1, 2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, [], masters)
    assert st.evaluate_stop(obj, grid, _BUDGET).state == "met"


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


def test_flagged_master_with_reliable_interval_converges():
    # The accept-flagged decision: a model_incomplete fit whose omega0 interval is reliable + tight
    # still delivers a usable omega0 for the tolerance objective.
    obj = _obj()
    masters = [_master((h, 0.0, 0.0), 0.30, disp="flagged") for h in (2.1, 2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, [], masters)
    assert st.evaluate_stop(obj, grid, _BUDGET).state == "met"


def test_unreliable_master_does_not_converge():
    # A gamma-at-bound collapse with no usable interval (reliable=False) must not certify, even tiny.
    obj = _obj()
    masters = [_master((h, 0.0, 0.0), 0.001, reliable=False) for h in (2.1, 2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, [], masters)
    assert st.evaluate_stop(obj, grid, _BUDGET).state != "met"


def test_single_production_converges_when_master_degraded():
    # Model-limited q: pooling degrades the fit so the master is junk (unusable), but a single
    # production fit is flagged + reliable + within tolerance → converge on the single fit.
    obj = _obj()
    obs = [_obs((h, 0.0, 0.0), "production", 0.3, disp="flagged") for h in (2.1, 2.2, 2.3, 2.4)]
    masters = [_master((h, 0.0, 0.0), 0.001, disp="junk", reliable=False) for h in (2.1, 2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert st.evaluate_stop(obj, grid, _BUDGET).state == "met"


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


def test_uncertifiable_delivery_is_third_state_then_stuck():
    # A delivered-but-uncertifiable production fit: no convergence, decline counter UNTOUCHED,
    # n_uncertifiable increments; after the budget the q is stuck with reason uncertifiable_interval,
    # and the why-string names γ-at-bound / uncertifiable, NOT "no signal".
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", 0.001, disp="flagged",
                certifiable=False, uncertifiable_kind="gamma_at_bound") for _ in range(st.MAX_DECLINES)]
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]   # the other three converge
    grid = st.summarize_grid(obj, obs, masters)
    g0 = grid[0]
    assert g0.n_decline == 0 and g0.n_uncertifiable == st.MAX_DECLINES
    assert not g0.converged and g0.stuck and g0.stuck_reason == "uncertifiable_interval"
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert d.state == "unmeetable"
    joined = " ".join(d.reasons).lower()
    assert "uncertifiable" in joined and "no signal" not in joined
    per_q = {tuple(q["hkl"]): q for q in d.to_dict()["per_q"]}
    assert per_q[(2.1, 0.0, 0.0)]["reason"] == "uncertifiable_interval"


def test_uncertifiable_does_not_reset_declines():
    # An uncertifiable delivery is NOT evidence of signal absence but also NOT a certifiable delivery:
    # it must neither increment nor RESET the no-signal decline counter.
    obj = _obj()
    obs = ([_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="junk"),
            _obs((2.1, 0.0, 0.0), "production", 0.001, disp="flagged",
                 certifiable=False, uncertifiable_kind="gamma_at_bound")])
    grid = st.summarize_grid(obj, obs)
    assert grid[0].n_decline == 1 and grid[0].n_uncertifiable == 1


def test_fast_tier_only_stuck_reason():
    # A q whose fits never left the untrusted fast tier → stuck with reason fast_tier_only.
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, disp="resolved",
                certifiable=False, uncertifiable_kind="fast_tier") for _ in range(st.MAX_DECLINES)]
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert grid[0].stuck and grid[0].stuck_reason == "fast_tier_only"
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert any("fast" in r.lower() for r in d.reasons)


def test_all_validation_rejected_declines_is_infeasible():
    # Every decline was a TAVI validation rejection (verdict validation_rejected) → the point is
    # unreachable by the instrument, not merely quiet: stuck reason `infeasible`, not `no_signal`.
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="declined")
           for _ in range(st.MAX_DECLINES)]
    for o in obs:
        o["verdict"] = "validation_rejected"
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert grid[0].n_infeasible == st.MAX_DECLINES and grid[0].n_decline == st.MAX_DECLINES
    assert grid[0].stuck and grid[0].stuck_reason == "infeasible"
    d = st.evaluate_stop(obj, grid, _BUDGET)
    joined = " ".join(d.reasons).lower()
    assert "unreachable by the instrument" in joined and "no signal" not in joined


def test_unmodeled_declines_is_signal_unmodeled():
    # Signal present but the model cannot fit it (disposition 'unmodeled') → stuck reason
    # `signal_unmodeled` (a model problem), never `no_signal`.
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="unmodeled")
           for _ in range(st.MAX_DECLINES)]
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert grid[0].n_unmodeled == st.MAX_DECLINES
    assert grid[0].stuck and grid[0].stuck_reason == "signal_unmodeled"
    d = st.evaluate_stop(obj, grid, _BUDGET)
    joined = " ".join(d.reasons).lower()
    assert "model cannot account for it" in joined and "no signal" not in joined


def test_mixed_declines_falls_back_to_no_signal():
    # A mix of a validation rejection and a plain junk decline is neither ALL-infeasible nor
    # unmodeled → the terminal reason stays `no_signal`.
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="declined"),
           _obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="declined"),
           _obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="junk")]
    obs[0]["verdict"] = obs[1]["verdict"] = "validation_rejected"
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert grid[0].n_infeasible == 2 and grid[0].n_decline == 3
    assert grid[0].stuck and grid[0].stuck_reason == "no_signal"


def test_no_usable_fits_is_still_no_signal():
    # Genuine no-usable-fits q → stuck reason no_signal (existing behavior preserved).
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="junk") for _ in range(3)]
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert grid[0].stuck and grid[0].stuck_reason == "no_signal"
    assert any("no signal" in r for r in st.evaluate_stop(obj, grid, _BUDGET).reasons)


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
    assert grid[1].best_prod_halfwidth == pytest.approx(0.1)
    assert grid[2].master is None
    assert grid[3].master is right_master


def test_branch_continuation_reports_branch_ambiguity_not_no_signal():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False,
                disp="branch_continuation") for _ in range(st.MAX_DECLINES)]
    for row in obs:
        row["verdict"] = "branch_continuation"
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert grid[0].stuck_reason == "branch_ambiguity"
    decision = st.evaluate_stop(obj, grid, _BUDGET)
    joined = " ".join(decision.reasons).lower()
    assert "branch ambiguity" in joined and "no-signal" in joined


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
def test_unmeetable_on_budget_exhaustion():
    obj = _obj()
    grid = st.summarize_grid(obj, [_obs((2.1, 0.0, 0.0), "quicklook", 0.9)])
    d = st.evaluate_stop(obj, grid, {"kind": "neutrons", "amount": 100.0, "spent": 100.0})
    assert d.state == "unmeetable"
    assert any("budget" in r for r in d.reasons)


def test_unmeetable_when_tolerance_below_floor():
    obj = _obj(tol=0.05)
    cal = Calibration.from_dict({"inflation": 2.0, "buckets": [], "center_pull_mean": 0.117})
    grid = st.summarize_grid(obj, [])
    d = st.evaluate_stop(obj, grid, _BUDGET, calibration=cal)
    assert d.state == "unmeetable"
    assert any("floor" in r for r in d.reasons)


def test_stuck_via_declines_is_skipped_not_campaign_fatal():
    # One q gives no signal 3x while others are still workable (unvisited): the campaign continues.
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="junk") for _ in range(3)]
    grid = st.summarize_grid(obj, obs)
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert d.state == "continue"
    assert grid[0].stuck and not grid[1].stuck


def test_stuck_via_max_prod_attempts():
    # A q pooled MAX_PROD_ATTEMPTS times with its master still over tolerance is abandoned.
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", 0.9) for _ in range(st.MAX_PROD_ATTEMPTS)]
    masters = [_master((2.1, 0.0, 0.0), 0.9)] + [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]
    grid = st.summarize_grid(obj, obs, masters)
    assert grid[0].stuck
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert d.state == "unmeetable"
    assert any("converged 3/4" in r for r in d.reasons)


def test_unmeetable_when_every_remaining_q_is_stuck():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="junk") for _ in range(3)]
    masters = [_master((h, 0.0, 0.0), 0.3) for h in (2.2, 2.3, 2.4)]   # the other three converge
    grid = st.summarize_grid(obj, obs, masters)
    d = st.evaluate_stop(obj, grid, _BUDGET)
    assert d.state == "unmeetable"
    assert any("no signal" in r for r in d.reasons)
    assert any("converged 3/4" in r for r in d.reasons)


def test_path_scalars_are_arclength():
    obj = _obj()
    s = st.path_scalars(obj.path)
    assert s == pytest.approx([0.0, 0.1, 0.2, 0.3])


def test_match_grid_index_rounds_to_nearest():
    obj = _obj()
    assert st.match_grid_index((2.2000001, 0.0, 0.0), obj.path) == 1
    assert st.match_grid_index((2.9, 0.0, 0.0), obj.path) is None
