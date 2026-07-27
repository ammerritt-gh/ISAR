"""Qt-free fit-display policy: revision-safe intervals, state grammar, and acquisition chronology."""
from __future__ import annotations

import pytest

from isar.workspace_api import (
    FitDisplayState,
    FitView,
    MasterAssessmentView,
    fit_display,
    latest_completed_acquisition,
    matching_assessment,
    resolve_interval,
)


def _fit(**updates) -> FitView:
    outcome = {
        "detector_id": "det",
        "q": [2.0, 0.0, 0.0],
        "omega0": 5.0,
        "interval": [4.7, 5.0, 5.6],
        "interval_reliable": True,
        "branch_id": "optic",
        "target_branch": "optic",
        **updates,
    }
    return FitView.from_outcome(outcome, scan_number=7, unit_id="S:7")


def _assessment(**updates) -> MasterAssessmentView:
    return MasterAssessmentView.from_dict({
        "hkl": [2.0, 0.0, 0.0],
        "assessment_state": "measured",
        "omega0": 5.1,
        "calibrated_halfwidth": 0.25,
        "master_revision": "rev-2",
        "track_state": "confirmed",
        "uncertainty_method": "bootstrap_calibrated",
        **updates,
    })


def test_fit_view_surfaces_acquisition_and_target_branch_from_both_sources():
    from_outcome = _fit(acquisition_id="acq-outcome", target_branch="acoustic")
    assert from_outcome.acquisition_id == "acq-outcome"
    assert from_outcome.target_branch == "acoustic"

    from_row = FitView.from_row(
        {"scan": "7", "detector": "det", "acquisition_id": "acq-row",
         "target_branch": "optic"},
        outcome={"acquisition_id": "acq-outcome", "target_branch": "acoustic"},
    )
    assert from_row.acquisition_id == "acq-row"
    assert from_row.target_branch == "optic"


def test_exact_master_revision_uses_latest_matching_assessment_interval():
    fit = _fit(pooled=True, master_revision="rev-2")
    older = _assessment(
        omega0=4.9, calibrated_halfwidth=0.4,
        uncertainty_method="profile_calibrated",
    )
    latest = _assessment()
    interval, assessment = resolve_interval(fit, [older, latest])
    assert assessment is latest
    assert interval is not None
    assert interval.lo == pytest.approx(4.85)
    assert interval.hi == pytest.approx(5.35)
    assert interval.source == "master_assessment"
    assert interval.method == "bootstrap_calibrated"


def test_populated_stale_revision_cannot_borrow_same_hkl_assessment():
    fit = _fit(pooled=True, master_revision="rev-1")
    current = _assessment(master_revision="rev-2")
    assert matching_assessment(fit, [current]) is None
    interval, assessment = resolve_interval(fit, [current])
    assert assessment is None
    assert interval is not None
    assert (interval.lo, interval.hi) == pytest.approx((4.7, 5.6))
    assert interval.source == "fit"
    assert interval.method == "bootstrap"


@pytest.mark.parametrize(
    "fit_revision, assessment_revision",
    [(None, "rev-2"), ("rev-2", None), (None, None)],
)
def test_hkl_fallback_is_limited_to_legacy_missing_revision(
        fit_revision, assessment_revision):
    fit = _fit(pooled=True, master_revision=fit_revision)
    assessment = _assessment(master_revision=assessment_revision)
    assert matching_assessment(fit, [assessment]) is assessment


def test_hkl_fallback_rejects_a_different_location():
    fit = _fit(pooled=True, master_revision=None)
    assessment = _assessment(hkl=[2.1, 0.0, 0.0], master_revision=None)
    assert matching_assessment(fit, [assessment]) is None


@pytest.mark.parametrize(
    "fit_updates, assessment, selected, expected",
    [
        ({}, None, True, FitDisplayState.INDIVIDUAL_VALID),
        ({"interval": None}, None, True, FitDisplayState.INDIVIDUAL_UNCERTIFIED),
        ({"interval_reliable": False}, None, True, FitDisplayState.INDIVIDUAL_UNRELIABLE),
        ({"interval_reliable": False, "omega0_profile": [4.8, 5.3]}, None, True,
         FitDisplayState.INDIVIDUAL_VALID),
        ({}, None, False, FitDisplayState.UNSELECTED_CANDIDATE),
        ({"pooled": True, "master_revision": "rev-2"}, "target", True,
         FitDisplayState.MASTER_TARGET),
        ({"pooled": True, "master_revision": "rev-2", "branch_id": None}, "target", True,
         FitDisplayState.MASTER_AMBIGUOUS),
        ({"pooled": True, "master_revision": "rev-2", "branch_id": "acoustic"}, "target", True,
         FitDisplayState.MASTER_MISMATCH),
        ({"pooled": True, "master_revision": "rev-2"}, "certified", True,
         FitDisplayState.MASTER_CERTIFIED),
        ({"pooled": True, "master_revision": "rev-1"}, "target", True,
         FitDisplayState.MASTER_UNASSESSED),
    ],
)
def test_fit_display_state_matrix(fit_updates, assessment, selected, expected):
    fit = _fit(**fit_updates)
    assessments = []
    if assessment == "target":
        assessments = [_assessment()]
    elif assessment == "certified":
        assessments = [_assessment(assessment_state="certified")]
    display = fit_display(
        fit, assessments, branch_tracks={"target_branch": "optic"}, selected=selected)
    assert display.state is expected


def test_converged_revision_matched_assessment_is_certified():
    fit = _fit(pooled=True, master_revision="rev-2")
    display = fit_display(fit, [_assessment(converged=True)])
    assert display.state is FitDisplayState.MASTER_CERTIFIED


def test_unreliable_bootstrap_falls_back_to_profile_then_trusted_hesse():
    profile = _fit(
        interval_reliable=False, omega0_profile=[4.8, 5.0, 5.3],
        hesse_interval=[4.9, 5.1], hesse_trusted=True)
    interval, assessment = resolve_interval(profile)
    assert assessment is None
    assert interval is not None
    assert (interval.lo, interval.hi) == pytest.approx((4.8, 5.3))
    assert interval.method == "profile"

    hesse = _fit(
        interval_reliable=False, omega0_profile=None,
        hesse_interval=[4.9, 5.1], hesse_trusted=True)
    interval, assessment = resolve_interval(hesse)
    assert assessment is None
    assert interval is not None
    assert (interval.lo, interval.hi) == pytest.approx((4.9, 5.1))
    assert interval.method == "hesse"


def test_provisional_master_identity_is_ambiguous():
    fit = _fit(pooled=True, master_revision="rev-2")
    display = fit_display(fit, [_assessment(track_state="provisional")])
    assert display.state is FitDisplayState.MASTER_AMBIGUOUS


def test_branch_track_target_overrides_persisted_fit_target():
    fit = _fit(
        pooled=True, master_revision="rev-2",
        target_branch="acoustic", branch_id="optic",
    )
    display = fit_display(
        fit, [_assessment()], branch_tracks={"target_branch": "optic"})
    assert display.target_branch == "optic"
    assert display.state is FitDisplayState.MASTER_TARGET


def _event(kind, **payload):
    return {"kind": kind, "payload": payload}


def test_latest_completed_acquisition_follows_submission_order_not_analysis_order():
    events = [
        _event("physical_submission", job_id="j1", acquisition_id="A1"),
        _event("physical_submission", job_id="j2", acquisition_id="A2"),
        _event("analyze", job_id="j2"),
        _event("analyze", job_id="j1"),
    ]
    selected = latest_completed_acquisition(events)
    assert selected.acquisition_id == "A2"
    assert selected.detailed_events_present is True


def test_latest_completed_acquisition_excludes_pending_failed_and_nonphysical_events():
    events = [
        _event("physical_submission", job_id="j1", acquisition_id="A1"),
        _event("analyze", job_id="j1"),
        _event("physical_submission", job_id="j2", acquisition_id="A2"),  # pending
        _event("analyze", job_id="certification-only"),  # no physical submission
        _event("physical_submission", job_id="j3", acquisition_id="A3"),
        _event("analyze", job_id="j3"),
        _event("exception", action="scan_not_done", job_id="j3"),
    ]
    selected = latest_completed_acquisition(events)
    assert selected.acquisition_id == "A1"
    assert selected.detailed_events_present is True


def test_latest_completed_acquisition_reports_legacy_event_absence():
    selected = latest_completed_acquisition([
        _event("analyze", job_id="j1"),
        _event("physical_submission", acquisition_id="legacy-without-job"),
    ])
    assert selected.acquisition_id is None
    assert selected.detailed_events_present is False
