"""Exception branch handlers: recenter math, escalation, flagged/held, budget, validation."""
import pytest

from isar.drive.supervisor import exceptions as exc


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


def test_validation_rejection_drops_unavailable_proposal():
    drop = exc.handle_validation_rejection(["all_points_infeasible"])
    assert drop.action == "drop"
    assert "no executable scan" in drop.reason
