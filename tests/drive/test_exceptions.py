"""Exception branch handlers: recenter math, escalation, flagged/held, budget, validation."""
import pytest

from isar.drive.supervisor import exceptions as exc


def test_recenter_centers_on_the_observed_peak_and_widens():
    # Peak intensity sits at deltaE = 6.0, near the window's high edge (window [-2, 2]).
    counts = [3.0, 5.0, 90.0]
    values = [-1.0, 2.0, 6.0]
    act = exc.handle_missed_window(counts, values, e_lo=-2.0, e_hi=2.0, attempt=0)
    assert act.action == "recenter"
    assert act.params["center"] == pytest.approx(6.0)
    old_width = 4.0
    new_width = act.params["e_hi"] - act.params["e_lo"]
    assert new_width == pytest.approx(old_width * exc.WIDEN_FACTOR)
    # Symmetric about the observed peak.
    assert 0.5 * (act.params["e_lo"] + act.params["e_hi"]) == pytest.approx(6.0)
    assert act.params["attempt"] == 1


def test_recenter_widens_blindly_when_no_counts():
    act = exc.handle_missed_window([None, None], [1.0, 2.0], e_lo=0.0, e_hi=4.0, attempt=0)
    assert act.action == "recenter"
    assert act.params["center"] == pytest.approx(2.0)     # old window center
    assert (act.params["e_hi"] - act.params["e_lo"]) > 4.0


def test_recenter_escalates_past_the_attempt_cap():
    act = exc.handle_missed_window([1, 2, 3], [0, 1, 2], e_lo=-2, e_hi=2,
                                   attempt=exc.MAX_RECENTER_ATTEMPTS)
    assert act.action == "escalate" and act.escalate is True


def test_flagged_accepts_number_but_does_not_converge():
    act = exc.handle_flagged("flagged", verdict="model_incomplete")
    assert act.action == "accept_no_converge"


def test_held_schedules_context_and_flags_unexpected_on_tavi():
    act = exc.handle_held("held_for_calibration")
    assert act.action == "schedule_context"
    assert act.escalate is True                            # should not happen on the TAVI tier


def test_budget_warn_and_exhausted():
    assert exc.handle_budget({"kind": "neutrons", "amount": 100.0, "spent": 10.0}) is None
    warn = exc.handle_budget({"kind": "neutrons", "amount": 100.0, "spent": 92.0})
    assert warn.action == "warn"
    done = exc.handle_budget({"kind": "neutrons", "amount": 100.0, "spent": 100.0})
    assert done.action == "exhausted"


def test_validation_rejection_repair_vs_drop():
    repair = exc.handle_validation_rejection(["infeasible_points: 2"])
    assert repair.action == "repair"
    drop = exc.handle_validation_rejection([], allow_partial_ok=True)
    assert drop.action == "drop"                           # nothing feasible to keep
    drop2 = exc.handle_validation_rejection(["bad"], allow_partial_ok=False)
    assert drop2.action == "drop"
