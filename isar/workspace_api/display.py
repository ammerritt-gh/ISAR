"""Qt-free fit-display semantics shared by GUI and report plot builders.

This module is the single seam for three related judgments: which durable master assessment
belongs to a fit, which interval may be displayed, and which composed glyph represents the fit.
It reads immutable workspace views and returns values; it never imports Qt or writes a workspace.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite


class FitDisplayState(str, Enum):
    INDIVIDUAL_VALID = "individual_valid"
    INDIVIDUAL_UNCERTIFIED = "individual_uncertified"
    INDIVIDUAL_UNRELIABLE = "individual_unreliable"
    MASTER_TARGET = "master_target"
    MASTER_AMBIGUOUS = "master_ambiguous"
    MASTER_MISMATCH = "master_mismatch"
    MASTER_CERTIFIED = "master_certified"
    MASTER_UNASSESSED = "master_unassessed"
    UNSELECTED_CANDIDATE = "unselected_candidate"


@dataclass(frozen=True)
class DisplayInterval:
    lo: float
    hi: float
    source: str
    method: str | None = None


@dataclass(frozen=True)
class FitDisplay:
    state: FitDisplayState
    interval: DisplayInterval | None
    assessment: object | None
    target_branch: str | None


@dataclass(frozen=True)
class AcquisitionSelection:
    acquisition_id: str | None
    detailed_events_present: bool


def latest_completed_acquisition(events) -> AcquisitionSelection:
    """Newest physical submission whose non-null job has a completed analysis event."""
    rows = list(events or ())
    analyzed = {
        str((row.get("payload") or {}).get("job_id"))
        for row in rows if row.get("kind") == "analyze"
        and (row.get("payload") or {}).get("job_id") is not None
    }
    failed = {
        str((row.get("payload") or {}).get("job_id"))
        for row in rows if row.get("kind") == "exception"
        and (row.get("payload") or {}).get("action") == "scan_not_done"
        and (row.get("payload") or {}).get("job_id") is not None
    }
    detailed = []
    for row in rows:
        if row.get("kind") != "physical_submission":
            continue
        payload = row.get("payload") or {}
        if payload.get("job_id") is not None and payload.get("acquisition_id") is not None:
            detailed.append(payload)
    for payload in reversed(detailed):
        job_id = str(payload["job_id"])
        if job_id in analyzed and job_id not in failed:
            return AcquisitionSelection(str(payload["acquisition_id"]), True)
    return AcquisitionSelection(None, bool(detailed))


def _same_hkl(left, right, *, atol: float = 1e-6) -> bool:
    if left is None or right is None or len(left) != 3 or len(right) != 3:
        return False
    try:
        return all(abs(float(a) - float(b)) <= atol for a, b in zip(left, right))
    except (TypeError, ValueError):
        return False


def matching_assessment(fit, assessments):
    """Return the latest assessment that can safely describe ``fit``.

    Populated revisions must agree exactly. HKL fallback is only for legacy data where at least one
    side has no revision; a stale populated revision can therefore never borrow a newer interval.
    """
    fit_revision = getattr(fit, "master_revision", None)
    rows = list(assessments or ())
    if fit_revision is not None:
        exact = [row for row in rows
                 if getattr(row, "master_revision", None) is not None
                 and str(row.master_revision) == str(fit_revision)]
        if exact:
            return exact[-1]
    for row in reversed(rows):
        row_revision = getattr(row, "master_revision", None)
        if fit_revision is not None and row_revision is not None:
            continue
        if _same_hkl(getattr(fit, "q", None), getattr(row, "hkl", None)):
            return row
    return None


def fit_certifying_interval(fit) -> DisplayInterval | None:
    """Fit-level certifying interval, with the fitter's established tier precedence."""
    candidates = (
        ("bootstrap", getattr(fit, "interval", None),
         getattr(fit, "interval_reliable", None) is not False),
        ("profile", getattr(fit, "omega0_profile", None), True),
        ("hesse", getattr(fit, "hesse_interval", None), bool(getattr(fit, "hesse_trusted", False))),
    )
    for method, interval, permitted in candidates:
        if not permitted or interval is None or len(interval) < 2:
            continue
        try:
            lo, hi = float(interval[0]), float(interval[-1])
        except (TypeError, ValueError):
            continue
        if isfinite(lo) and isfinite(hi) and hi >= lo:
            return DisplayInterval(lo, hi, "fit", method)
    return None


def resolve_interval(fit, assessments=()) -> tuple[DisplayInterval | None, object | None]:
    """Resolve the authoritative displayed interval and its matched assessment."""
    assessment = matching_assessment(fit, assessments) if getattr(fit, "pooled", False) else None
    if assessment is not None:
        omega0 = getattr(assessment, "omega0", None)
        halfwidth = getattr(assessment, "halfwidth", None)
        try:
            omega0, halfwidth = float(omega0), float(halfwidth)
        except (TypeError, ValueError):
            omega0 = halfwidth = None
        if (omega0 is not None and halfwidth is not None and isfinite(omega0)
                and isfinite(halfwidth) and halfwidth >= 0.0):
            return (
                DisplayInterval(
                    omega0 - halfwidth, omega0 + halfwidth, "master_assessment",
                    getattr(assessment, "uncertainty_method", None) or "calibrated_halfwidth"),
                assessment,
            )
    return fit_certifying_interval(fit), assessment


def _target_branch(branch_tracks, fit) -> str | None:
    if isinstance(branch_tracks, dict) and branch_tracks.get("target_branch") not in (None, ""):
        return str(branch_tracks["target_branch"])
    if isinstance(branch_tracks, dict):
        targets = {
            str(track.get("branch_id"))
            for track in (branch_tracks.get("tracks") or ())
            if isinstance(track, dict) and str(track.get("role") or "").lower() == "target"
            and track.get("branch_id") not in (None, "")
        }
        if len(targets) == 1:
            return next(iter(targets))
    value = getattr(fit, "target_branch", None)
    return None if value in (None, "") else str(value)


def fit_display(fit, assessments=(), *, branch_tracks=None, selected: bool = True) -> FitDisplay:
    """Return the complete display judgment for one fit through one small pure interface."""
    if not selected:
        return FitDisplay(FitDisplayState.UNSELECTED_CANDIDATE, None, None, None)

    interval, assessment = resolve_interval(fit, assessments)
    target = _target_branch(branch_tracks, fit)
    if not getattr(fit, "pooled", False):
        if interval is not None:
            state = FitDisplayState.INDIVIDUAL_VALID
        elif getattr(fit, "interval_reliable", None) is False:
            state = FitDisplayState.INDIVIDUAL_UNRELIABLE
        else:
            state = FitDisplayState.INDIVIDUAL_UNCERTIFIED
        return FitDisplay(state, interval, None, target)

    if assessment is None:
        return FitDisplay(FitDisplayState.MASTER_UNASSESSED, interval, None, target)

    track_state = str(
        getattr(assessment, "track_state", None) or getattr(fit, "track_state", None) or "").lower()
    branch_id = getattr(fit, "branch_id", None)
    if track_state in {"ambiguous", "missing", "provisional", "blocked", "frontier_blocked"} \
            or target is None or branch_id in (None, ""):
        state = FitDisplayState.MASTER_AMBIGUOUS
    elif str(branch_id) != target:
        state = FitDisplayState.MASTER_MISMATCH
    elif str(getattr(assessment, "state", "")).lower() == "certified" \
            or bool(getattr(assessment, "converged", False)):
        state = FitDisplayState.MASTER_CERTIFIED
    else:
        state = FitDisplayState.MASTER_TARGET
    return FitDisplay(state, interval, assessment, target)
