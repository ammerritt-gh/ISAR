"""Exception handling — the non-routine branches (LLM_HARNESS_DESIGN §2.1, §8 step 4.6).

The supervisor owns the events that are not a routine next-point, reasoning about each as a **typed
action** rather than crashing:

- **missed window** — the peak fell outside the chosen energy window (ISAR ``declined`` / ``no_signal``).
  Recovery: LOOK at the raw counts array to re-center the window on the observed intensity and widen it
  (a LOOK, not a conclusion — §1: the counts re-center the *next* scan, they never become a recorded
  number), up to :data:`MAX_RECENTER_ATTEMPTS` before escalating.
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

MAX_RECENTER_ATTEMPTS = 3
WIDEN_FACTOR = 1.5
BUDGET_WARN_FRACTION = 0.9
# Recentering onto |ΔE| ≲ this many resolution FWHM of the elastic line is refused: on a real
# instrument that is beam spent pointing at the elastic peak (MVP-16 re-aimed at ΔE = 0.009 meV).
ELASTIC_GUARD_K = 1.0

# ISAR dispositions/verdicts that mean "no usable peak in this window". `elastic_only` (SCENE-1
# PR-a) is the phonon-less elastic+bg winner: for the driver's "did this window catch the peak"
# question it is the same miss as no_signal (only elastic scattering was resolvable there).
MISSED_VERDICTS = frozenset({"no_signal", "declined", "elastic_only"})
# ISAR statuses that mean "held pending a context scan".
HELD_STATUSES = frozenset({"held_for_calibration", "held_for_sample"})


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


def _peak_deltaE(counts: Sequence, scan_values: Sequence):
    """The deltaE of the largest count (a LOOK — control feedback, never a recorded number).

    Tolerates ``None`` entries (TAVI marks unmeasured/invalid points ``null``, never ``0``). Returns
    ``None`` when there is nothing to look at.
    """
    best_v, best_e = None, None
    for e, c in zip(scan_values, counts):
        if c is None or e is None:
            continue
        cv = float(c)
        if best_v is None or cv > best_v:
            best_v, best_e = cv, float(e)
    return best_e


def handle_missed_window(counts: Sequence, scan_values: Sequence, *, e_lo: float, e_hi: float,
                         attempt: int, max_attempts: int = MAX_RECENTER_ATTEMPTS,
                         widen: float = WIDEN_FACTOR, resolution_fwhm: float | None = None,
                         guard_k: float = ELASTIC_GUARD_K) -> ExceptionAction:
    """Recover a missed energy window: re-center on the observed peak and widen; escalate past the cap.

    ``attempt`` is the number of recovery attempts already made at this q (0 on the first miss). When it
    reaches ``max_attempts`` the action escalates instead of re-centering. The new window keeps the same
    intent — center on where the intensity actually is, widen by ``widen`` — and is a LOOK: the counts
    steer the next scan, they are never turned into an ω₀.

    **Elastic guard.** When ``resolution_fwhm`` is supplied and the observed peak sits within
    ``guard_k`` × FWHM of ΔE = 0 (the elastic line), re-centering onto it is *refused*: the peak is the
    elastic peak, not the phonon, and re-aiming there is beam spent pointing at ΔE ≈ 0. The action is
    ``recenter_refused_elastic`` — still a bounded retry (the window is widened about the OLD center,
    never re-aimed at elastic, never an instant abandon). ``resolution_fwhm=None`` restores the exact
    pre-guard behavior (back-compat).

    **Peak-inside-window.** When the (non-elastic) Stokes-side peak already lies inside the current
    window by at least a margin (the greater of ``resolution_fwhm`` or 10% of the window width), the
    window is left UNCHANGED and the action is ``peak_inside_window``: widening cannot help find a
    peak that is already inside — the scan saw real signal the window covers, so the miss lies
    elsewhere (e.g. a modelling gap) and the planner's next action, not another widen, is the recovery.
    """
    if attempt >= max_attempts:
        return ExceptionAction(
            "escalate",
            f"missed the energy window {max_attempts}× at this q — escalating to the supervisor/human "
            "instead of burning more beam", {"attempt": attempt}, escalate=True)

    width = float(e_hi) - float(e_lo)
    new_width = width * float(widen)
    old_center = 0.5 * (float(e_lo) + float(e_hi))
    peak = _peak_deltaE(counts, scan_values)
    if peak is None:
        # Nothing to see — widen symmetrically about the old center and try again.
        center = old_center
        reason = "no counts to re-center on; widening the window symmetrically and retrying"
    else:
        # Reflect an anti-Stokes peak to the Stokes side: the DHO is two-sided (kT ≫ ħω here, both
        # branches comparable) but the analysis fits the positive-deltaE branch, so a window
        # recentered onto -|ω₀| would carry no Stokes coverage at all.
        center = abs(peak)
        if resolution_fwhm is not None and center <= guard_k * float(resolution_fwhm):
            # The observed peak IS the elastic line — refuse to re-aim at it. Widen about the OLD
            # window center (a bounded retry that keeps some Stokes coverage) rather than chase ΔE ≈ 0.
            new_lo = old_center - new_width / 2.0
            new_hi = old_center + new_width / 2.0
            return ExceptionAction(
                "recenter_refused_elastic",
                f"proposed re-center onto deltaE={center:g} meV is within the elastic guard "
                f"({guard_k:g}× resolution FWHM {float(resolution_fwhm):g} meV) — that is the elastic "
                f"line, not the phonon; widening about the old center ({old_center:g} meV) instead",
                {"proposed_center": center, "resolution_fwhm": float(resolution_fwhm),
                 "guard_k": float(guard_k), "e_lo": new_lo, "e_hi": new_hi, "center": old_center,
                 "attempt": attempt + 1, "widen": float(widen)})
        margin = max(float(resolution_fwhm) if resolution_fwhm is not None else 0.0, 0.1 * width)
        lo, hi = float(e_lo), float(e_hi)
        # Only once the elastic guard above has had a resolution to clear the peak with: without
        # one, an elastic peak at ΔE ≈ 0 would read as "inside the window" and suppress the bounded
        # retry, so ``resolution_fwhm=None`` keeps the exact pre-guard behaviour below.
        if resolution_fwhm is not None and lo + margin <= center <= hi - margin:
            # The peak is already inside the window (well clear of both edges) — widening cannot help
            # find it, it's already found. Leave the window unchanged and return to the planner.
            return ExceptionAction(
                "peak_inside_window",
                f"peak observed at deltaE={center:g} meV lies inside the current window "
                f"[{lo:g}, {hi:g}]; widening cannot help — leaving the window and returning to "
                "the planner",
                {"peak": peak, "center": center, "e_lo": lo, "e_hi": hi, "attempt": attempt},
                escalate=False)
        reason = (f"peak observed at deltaE={peak:g} meV, outside/near the window edge; "
                  f"re-centering on the Stokes side ({center:g} meV) and widening")
    new_lo = center - new_width / 2.0
    new_hi = center + new_width / 2.0
    return ExceptionAction("recenter", reason,
                           {"e_lo": new_lo, "e_hi": new_hi, "center": center,
                            "attempt": attempt + 1, "widen": float(widen)})


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
