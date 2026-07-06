"""Exception handling — the non-routine branches (STUB — LLM_HARNESS_DESIGN §2.1, §8 step 4.6).

Contract: the supervisor owns the events that are not a routine next-point, reasoning about each as a
branch rather than crashing:

- **missed window** — the peak fell outside the chosen energy window (ISAR returns a
  ``declined``/``no_signal`` verdict). Recovery: widen the window and/or re-center on the observed
  intensity (a LOOK, not a conclusion — §1), or escalate if it keeps missing.
- **flagged / held** — an ISAR ``flagged`` or ``held_for_calibration``/``held_for_sample`` verdict.
  For a *held* status the supervisor schedules the missing context scan first (a resolution/Bragg
  scan, or a sample-defining alignment) before ISAR will fit the phonon scans (§4).
- **budget** — budget nearly/fully spent: renegotiate with the user (a concrete choice), do not
  silently burn the remainder.
- **validation rejection** — TAVI ``POST /validate`` returned ``would_queue == false``: repair the
  scan (fix or ``allow_partial`` the infeasible points) or drop the point.
- **σ-calibration drift** — the applied inflation no longer matches; trigger a re-calibration.

These map to the ``exception`` events of :mod:`isar.drive.session_log`. Not implemented this round.
"""
from __future__ import annotations


class DriverException(Exception):
    """Base for driver-side exceptional conditions the supervisor branches on (not errors to crash on)."""


class MissedWindow(DriverException):
    """The peak landed outside the chosen energy window (ISAR declined / no_signal)."""


class HeldForContext(DriverException):
    """ISAR held the scan pending a calibration/sample context scan the driver must schedule first."""


class BudgetExhausted(DriverException):
    """The campaign budget is spent with the path still under-determined."""


class ValidationRejected(DriverException):
    """TAVI rejected the proposed scan (would_queue == false); carries the blockers list."""
