"""The stopping criterion: master (pooled) convergence + unmeetable branches."""
import pytest

from isar.drive.acquisition.calibration import Calibration
from isar.drive.objective import ObjectiveSpec
from isar.drive.supervisor import stopping as st

_BUDGET = {"kind": "neutrons", "amount": 1e12, "spent": 0.0}


def _obj(tol=0.5):
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=tol,
                                 budget={"kind": "neutrons", "amount": 1e12})


def _obs(hkl, kind, hw, resolved=True, disp="resolved"):
    return {"hkl": hkl, "kind": kind, "calibrated_halfwidth": hw, "resolved": resolved,
            "disposition": disp, "omega0": 3.0 if resolved else None}


def _master(hkl, hw, disp="resolved", reliable=True, n=2, members=None):
    # A pooled-fit convergence row as loop._master_obs_from_row builds it.
    return {"hkl": hkl, "omega0": 3.0, "halfwidth": hw, "reliable": reliable,
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
