"""The stopping criterion: two-consecutive-confirm peeking guard + unmeetable branches."""
import pytest

from isar.drive.acquisition.calibration import Calibration
from isar.drive.objective import ObjectiveSpec
from isar.drive.supervisor import stopping as st


def _obj(tol=0.5):
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=tol,
                                 budget={"kind": "neutrons", "amount": 1e12})


def _pf(hw, resolved=True):
    return {"halfwidth": hw, "resolved": resolved}


# --------------------------------------------------------------------------- convergence core
def test_single_lucky_crossing_does_not_converge():
    # One production fit inside tolerance is NOT enough — the peeking guard needs a confirm.
    assert not st.q_converged([_pf(0.3)], 0.5)


def test_two_consecutive_confirm_converges():
    assert st.q_converged([_pf(0.3), _pf(0.35)], 0.5)


def test_first_crossing_needs_the_margin():
    # 0.45 is within tolerance but 1.2*0.45 = 0.54 > 0.5, so the first crossing does not count.
    assert not st.q_converged([_pf(0.45), _pf(0.45)], 0.5)
    # A tighter first crossing (0.40 -> 0.48 <= 0.5) with a confirm within tolerance converges.
    assert st.q_converged([_pf(0.40), _pf(0.48)], 0.5)


def test_unresolved_never_converges():
    assert not st.q_converged([_pf(0.3), _pf(0.3, resolved=False)], 0.5)


def test_next_kind_ladder():
    assert st.next_kind_for_q([], 0.5) == "production"
    assert st.next_kind_for_q([_pf(0.6)], 0.5) == "production"         # crossing not met
    assert st.next_kind_for_q([_pf(0.3)], 0.5) == "confirm"            # resolved first crossing
    assert st.next_kind_for_q([_pf(0.3), _pf(0.35)], 0.5) is None      # converged


# --------------------------------------------------------------------------- grid + decision
def _obs(hkl, kind, hw, resolved=True, disp="resolved"):
    return {"hkl": hkl, "kind": kind, "calibrated_halfwidth": hw, "resolved": resolved,
            "disposition": disp, "omega0": 3.0 if resolved else None}


def test_met_when_all_grid_q_converged():
    obj = _obj()
    obs = []
    for h in (2.1, 2.2, 2.3, 2.4):
        obs.append(_obs((h, 0.0, 0.0), "production", 0.30))
        obs.append(_obs((h, 0.0, 0.0), "confirm", 0.35))
    grid = st.summarize_grid(obj, obs)
    d = st.evaluate_stop(obj, grid, {"kind": "neutrons", "amount": 1e12, "spent": 0.0})
    assert d.state == "met"


def test_continue_when_underdetermined():
    obj = _obj()
    grid = st.summarize_grid(obj, [_obs((2.1, 0.0, 0.0), "quicklook", 0.9)])
    d = st.evaluate_stop(obj, grid, {"kind": "neutrons", "amount": 1e12, "spent": 0.0})
    assert d.state == "continue"


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
    d = st.evaluate_stop(obj, grid, {"kind": "neutrons", "amount": 1e12, "spent": 0.0},
                         calibration=cal)
    assert d.state == "unmeetable"
    assert any("floor" in r for r in d.reasons)


def test_unmeetable_on_repeated_declines():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "production", None, resolved=False, disp="declined") for _ in range(3)]
    grid = st.summarize_grid(obj, obs)
    d = st.evaluate_stop(obj, grid, {"kind": "neutrons", "amount": 1e12, "spent": 0.0}, max_declines=3)
    assert d.state == "unmeetable"
    assert any("declined" in r for r in d.reasons)


def test_path_scalars_are_arclength():
    obj = _obj()
    s = st.path_scalars(obj.path)
    assert s == pytest.approx([0.0, 0.1, 0.2, 0.3])


def test_match_grid_index_rounds_to_nearest():
    obj = _obj()
    assert st.match_grid_index((2.2000001, 0.0, 0.0), obj.path) == 1
    assert st.match_grid_index((2.9, 0.0, 0.0), obj.path) is None
