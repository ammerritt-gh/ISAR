"""Exception handling — the non-routine branches (LLM_HARNESS_DESIGN §2.1, §8 step 4.6).

The supervisor owns the events that are not a routine next-point, reasoning about each as a **typed
action** rather than crashing:

- **flagged / model-incomplete** — accept ISAR's number but do NOT count the q as converged.
- **held for context** — schedule the missing calibration/sample scan first (§4). On the TAVI tier this
  should not happen; if it does, log it honestly.
- **budget** — warn as the budget nears the cap; report exhaustion (the stopping criterion turns this
  into ``unmeetable``).
- **validation rejection** — TAVI ``would_queue == false``: drop the unavailable proposal.

Handlers are pure: they return an :class:`ExceptionAction` the loop acts on and logs as an ``exception``
event (:mod:`isar.drive.session_log`). None of them fits a peak or produces a number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

BUDGET_WARN_FRACTION = 0.9

# ISAR dispositions/verdicts that mean "no usable peak in this window". `elastic_only` (SCENE-1
# PR-a) is the phonon-less elastic+bg winner: for the driver's "did this window catch the peak"
# question it is the same miss as no_signal (only elastic scattering was resolvable there). Such a
# scan is a CLEAN NULL and ordinary evidence of absence over its window — there is no
# recentre-and-widen recovery any more; the EIG planner prices the null and decides again.
MISSED_VERDICTS = frozenset({"no_signal", "declined", "elastic_only"})
# ISAR statuses that mean "held pending a context scan".
HELD_STATUSES = frozenset({"held_for_calibration", "held_for_sample"})


class DriverException(Exception):
    """Base for driver-side exceptional conditions the supervisor branches on (not errors to crash on)."""


class HeldForContext(DriverException):
    """ISAR held the scan pending a calibration/sample context scan the driver must schedule first."""


class BudgetExhausted(DriverException):
    """The campaign budget is spent with the path still under-determined."""


class ValidationRejected(DriverException):
    """TAVI rejected the proposed scan (would_queue == false); carries the blockers list."""


@dataclass
class ExceptionAction:
    """A typed recovery action the loop executes and logs.

    ``action`` vocabulary: ``recenter`` (re-run with a new window), ``escalate`` (hand to the human /
    give up on this q), ``accept_no_converge`` (keep the number, do not converge), ``schedule_context``
    (run a calibration/sample scan first), ``warn`` / ``exhausted`` (budget), ``repair`` (modify a
    rejected proposal and retry it), and ``drop`` (skip this point without resubmitting it).
    """

    action: str
    reason: str
    params: dict[str, Any] = field(default_factory=dict)
    escalate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "reason": self.reason, "params": dict(self.params),
                "escalate": self.escalate}


def handle_flagged(disposition: str, *, verdict: str | None = None) -> ExceptionAction:
    """A ``flagged`` / model-incomplete ISAR result: accept the number but do not converge on it."""
    return ExceptionAction(
        "accept_no_converge",
        f"ISAR disposition {disposition!r} (verdict {verdict!r}): keep the number for the record but "
        "do not count this q as converged", {"disposition": disposition, "verdict": verdict})


def handle_held(status: str) -> ExceptionAction:
    """A ``held_for_*`` status: schedule the missing context scan first.

    On the TAVI tier a held status should not occur (there is no un-calibrated energy axis / missing
    sample context), so when it does we escalate and log it honestly rather than silently proceeding.
    """
    return ExceptionAction(
        "schedule_context",
        f"ISAR {status!r}: schedule the missing context scan before the phonon scans"
        " (unexpected on the TAVI tier — logging honestly)",
        {"status": status}, escalate=True)


def handle_budget(budget_state: dict, *, warn_fraction: float = BUDGET_WARN_FRACTION) -> ExceptionAction | None:
    """Warn as the budget nears the cap; report exhaustion. Returns ``None`` when comfortably within."""
    amount = budget_state.get("amount")
    spent = float(budget_state.get("spent", 0.0))
    if amount is None:
        return None
    amount = float(amount)
    if amount <= 0:
        return None
    frac = spent / amount
    if frac >= 1.0:
        return ExceptionAction("exhausted",
                               f"budget exhausted ({spent:g}/{amount:g} {budget_state.get('kind','')})",
                               {"spent": spent, "amount": amount, "fraction": frac})
    if frac >= warn_fraction:
        return ExceptionAction("warn",
                               f"budget {frac*100:.0f}% spent ({spent:g}/{amount:g} "
                               f"{budget_state.get('kind','')}) — renegotiate before over-running",
                               {"spent": spent, "amount": amount, "fraction": frac})
    return None


def handle_validation_rejection(blockers: Sequence[str]) -> ExceptionAction:
    """A ``would_queue == false`` means no requested point can run."""
    blockers = list(blockers or [])
    return ExceptionAction("drop",
                           f"validation rejected ({len(blockers)} blocker(s)); no executable "
                           "scan remains", {"blockers": blockers})
