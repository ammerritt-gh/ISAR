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


def test_recenter_refused_when_peak_is_the_elastic_line():
    # The MVP-16 failure: the biggest count sits at deltaE = 0.009 meV — that is the elastic peak, not
    # the phonon. With a resolution FWHM of 0.3 meV the proposed re-center (0.009 ≤ 1.0×0.3) is REFUSED;
    # the window is instead widened about the OLD center, never re-aimed at the elastic line.
    counts = [3.0, 500.0, 4.0]
    values = [-0.5, 0.009, 0.5]
    old_lo, old_hi = 2.0, 6.0
    act = exc.handle_missed_window(counts, values, e_lo=old_lo, e_hi=old_hi, attempt=0,
                                   resolution_fwhm=0.3)
    assert act.action == "recenter_refused_elastic"
    old_center = 0.5 * (old_lo + old_hi)
    new_center = 0.5 * (act.params["e_lo"] + act.params["e_hi"])
    assert new_center == pytest.approx(old_center)           # widened about the OLD center, not 0.009
    assert new_center != pytest.approx(0.009)
    assert act.params["proposed_center"] == pytest.approx(0.009)
    assert act.params["resolution_fwhm"] == pytest.approx(0.3)
    assert act.params["guard_k"] == pytest.approx(exc.ELASTIC_GUARD_K)
    # Still a bounded retry: widened by the widen factor, attempt advanced.
    assert (act.params["e_hi"] - act.params["e_lo"]) == pytest.approx((old_hi - old_lo) * exc.WIDEN_FACTOR)
    assert act.params["attempt"] == 1


def test_recenter_allowed_when_peak_is_a_real_phonon():
    # A peak well above the resolution FWHM is a genuine phonon — normal recenter, no refusal.
    counts = [3.0, 5.0, 90.0]
    values = [-1.0, 1.0, 3.0]
    act = exc.handle_missed_window(counts, values, e_lo=-2.0, e_hi=2.0, attempt=0,
                                   resolution_fwhm=0.3)
    assert act.action == "recenter"
    assert act.params["center"] == pytest.approx(3.0)


def test_recenter_guard_none_fwhm_is_byte_identical_to_pre_change():
    # Back-compat: resolution_fwhm=None reproduces the old behavior exactly, even for a near-elastic peak.
    counts = [3.0, 500.0, 4.0]
    values = [-0.5, 0.009, 0.5]
    guarded = exc.handle_missed_window(counts, values, e_lo=2.0, e_hi=6.0, attempt=0,
                                       resolution_fwhm=None)
    plain = exc.handle_missed_window(counts, values, e_lo=2.0, e_hi=6.0, attempt=0)
    assert guarded.action == plain.action == "recenter"
    assert guarded.params == plain.params
    assert guarded.params["center"] == pytest.approx(0.009)   # unguarded → re-aims at the elastic peak


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


def test_validation_rejection_drops_unavailable_proposal():
    drop = exc.handle_validation_rejection(["all_points_infeasible"])
    assert drop.action == "drop"
    assert "no executable scan" in drop.reason
