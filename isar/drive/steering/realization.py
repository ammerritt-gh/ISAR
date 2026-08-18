"""Turning a chosen :class:`MeasurementIntent` into an executable request (SCENE-4.3 C5).

The planner decides WHICH scientific question to answer next; nothing in the discovery layer says
how to *count* it. This module holds that one translation, and only it: the energy grid, the
exposure, and the assembly of the :class:`~isar.drive.acquisition.ScanProposal` the existing
submission path already knows how to finalize, validate, trim and submit.

**Provisional v1, SCENE-7-calibrated (owner ruling 2026-08-18).** Every number here is a *named
default* resolved through the Topic-4/5 precedence chain — user ask > card policy > ISAR default —
and frozen into the compiled program at compile time like every other threshold. They are named
provisional because nobody has calibrated them against a real instrument yet; they are not
"temporary code constants" to be quietly tuned in place.

The energy grid
---------------
Two rules, both owner-ruled:

1. **The step snaps to the nice-number grid** ``{1, 0.5, 0.25, 0.2} × 10^n`` meV — the largest
   grid value at or below the instrument's energy FWHM at the window centre ("a step about the
   FWHM"). A raw FWHM makes an unreadable request (``deltaE 0.37 2.9 0.31447``); a snapped one
   makes a scan a human can read off a log line.
2. **The points are ZERO-ANCHORED** — every point sits at ``k × step`` for integer ``k``, and the
   realized scan takes every such point INSIDE the intent's window. The first and last points are
   therefore the aligned values within the window, never the window's own endpoints. Anchoring at
   ``e_lo`` instead would put the whole grid at an arbitrary phase, and since every value in the
   nice-number grid divides 1 integrally, zero phase is exactly what makes the scan pass through
   the integer energies: ``[0.37, 2.9]`` at step ``0.25`` realizes ``0.50 … 2.75``, hitting 1.0
   and 2.0 dead on.

A window that contains fewer than two aligned points is REFUSED
(:class:`DiscoveryWindowUnrealizable`), never widened to the nearest aligned point outside it.
Widening would measure something the question did not ask about, and a one-point scan is not a
scan anyway: ``isar.drive.acquisition.physical_windows`` rejects it as ``degenerate_window`` and
TAVI itself refuses a single-point scan command with an HTTP 400.
"""
from __future__ import annotations

import math

from isar.drive.acquisition.acquire import ScanProposal

__all__ = [
    "NICE_STEP_MANTISSAS",
    "DiscoveryWindowUnrealizable", "aligned_window", "proposal_from_intent", "snap_energy_step",
]

#: The nice-number mantissas, each scaled by a power of ten. Every one divides 1 integrally, which
#: is what makes the zero-anchored grid pass through the integers.
NICE_STEP_MANTISSAS = (1.0, 0.5, 0.25, 0.2)

#: Decades searched around the FWHM. Covers every physically meaningful energy resolution
#: (0.001 meV to 100 meV) with room to spare, and stops at the low end where the round-to-12 below
#: still keeps all four mantissas: a smaller decade only manufactures candidates that round to 0.0
#: and are filtered straight back out.
_DECADES = range(-11, 13)

#: The scan kind a discovery survey point is submitted under. ``quicklook`` is the existing
#: survey-grade kind: it is excluded from ``PRODUCTION_KINDS`` (no full-tier escalation) and gets
#: the quicklook exposure floor in ``_finalize_proposal``.
DISCOVERY_SCAN_KIND = "quicklook"

_EPSILON = 1e-9


class DiscoveryWindowUnrealizable(ValueError):
    """The intent's window holds fewer than two zero-anchored grid points at the chosen step."""


def snap_energy_step(fwhm_meV: float) -> float:
    """The largest ``{1, 0.5, 0.25, 0.2} × 10^n`` value at or below ``fwhm_meV`` (meV).

    Monotonic by construction (a maximum over a fixed candidate set), so a larger resolution never
    yields a finer step. A non-finite or non-positive FWHM is a programming error here — the
    resolution seam already substitutes its own default for an instrument refusal — so it raises
    rather than quietly picking a step.
    """
    width = float(fwhm_meV)
    if not math.isfinite(width) or width <= 0.0:
        raise ValueError(f"energy FWHM must be a positive finite number of meV, got {fwhm_meV!r}")
    candidates = [round(mantissa * 10.0 ** decade, 12)
                  for decade in _DECADES for mantissa in NICE_STEP_MANTISSAS]
    admissible = [value for value in candidates if 0.0 < value <= width * (1.0 + _EPSILON)]
    if not admissible:
        raise ValueError(f"no nice-number step at or below {width!r} meV")
    return max(admissible)


def aligned_window(e_lo: float, e_hi: float, step: float) -> tuple[float, float, int]:
    """``(first, last, n_points)`` for the zero-anchored grid clipped INTO ``[e_lo, e_hi]``.

    ``first`` and ``last`` are multiples of ``step``, so the realized scan's endpoints are the
    aligned values inside the window rather than the window's own (possibly non-integer) bounds.
    Raises :class:`DiscoveryWindowUnrealizable` when fewer than two aligned points fit — see the
    module docstring for why that refuses instead of widening.

    A window straddling zero therefore includes the point at ΔE = 0 itself, on the elastic line.
    Whether a discovery scan may sit on the zone centre's elastic exclusion is the on-ZC question
    the owner's cold-start ruling assigns to the 4.4 acceptance script (SCENE4_EXECUTION_PLAN,
    "Cold-start ruling" item 4) — deliberately NOT decided here.
    """
    lo, hi, size = float(e_lo), float(e_hi), float(step)
    if not (math.isfinite(lo) and math.isfinite(hi) and math.isfinite(size)) or size <= 0.0:
        raise ValueError(f"cannot align window [{e_lo!r}, {e_hi!r}] on step {step!r}")
    if hi < lo:
        lo, hi = hi, lo
    # The tolerance is RELATIVE to the step, so an endpoint that is an aligned value up to floating
    # error (0.75 stored as 0.7499999999999999) is counted in rather than silently dropped.
    tolerance = _EPSILON * max(1.0, abs(lo / size), abs(hi / size))
    k_lo = math.ceil(lo / size - tolerance)
    k_hi = math.floor(hi / size + tolerance)
    n_points = k_hi - k_lo + 1
    if n_points < 2:
        raise DiscoveryWindowUnrealizable(
            f"window [{lo:g}, {hi:g}] meV holds {max(n_points, 0)} point(s) of the zero-anchored "
            f"{size:g} meV grid; at least two are needed for a scan. The window is not widened to "
            "reach an aligned point outside it — that would measure something this question did "
            "not ask about.")
    return round(k_lo * size, 12), round(k_hi * size, 12), n_points


def proposal_from_intent(intent, *, fwhm_meV: float, provenance: dict,
                         hypotheses_fingerprint: str | None,
                         reason: str, ncount: int) -> ScanProposal:
    """The executable request for one selected discovery action.

    Every field's source is named, because "where did this number come from" is the question a
    replay asks:

    ``h``/``k``/``l``
        the intent's ``hkl``, verbatim.
    ``e_lo``/``e_hi``/``n_points``
        the intent's ``[energy_lo, energy_hi]`` window CLIPPED to the zero-anchored grid.
    ``e_step``
        :func:`snap_energy_step` of ``fwhm_meV`` — the instrument resolution at the window centre.
    ``ncount``
        the exposure the CALLER read off the step's frozen ``discovery_monitor_ncount`` threshold
        — neutrons on the monitor, the wire's own currency. Required, with no module default: a
        default here would be exactly the read-time constant the owner's ruling removed.
    ``e_pred``
        the intent's own centre when it declares one, else the aligned window's midpoint. This is
        the energy the request is *about*, and the feasibility trim reads it to decide which
        reachable run to keep.
    ``meta``
        the campaign-provenance block (C1: ``topology_role`` / ``program_fingerprint`` /
        ``program_step`` / ``path_fingerprint``), the scientific action fingerprint, and the
        hypotheses fingerprint. The request fingerprint is NOT stamped here — ``_finalize_proposal``
        stamps it after the point-count and affordability clamps, so it always describes what was
        actually asked for.

    An intent with no energy window is refused: a discovery action with nothing to scan is a
    generator defect, not a request to guess a window for.
    """
    if intent.energy_lo is None or intent.energy_hi is None:
        raise DiscoveryWindowUnrealizable(
            f"intent {intent.scientific_action_fingerprint[:12]} ({intent.action_class}) declares "
            "no energy window; a discovery request cannot invent one")
    step = snap_energy_step(fwhm_meV)
    e_lo, e_hi, n_points = aligned_window(intent.energy_lo, intent.energy_hi, step)
    centre = (intent.window_center_meV if intent.window_center_meV is not None
              else 0.5 * (e_lo + e_hi))
    meta = {
        "scientific_action_fingerprint": intent.scientific_action_fingerprint,
        "hypotheses_fingerprint": hypotheses_fingerprint,
        "acquisition_action_role": "discovery",
        "evidence_sink": intent.evidence_sink,
        "discovery_energy_step_source": "nice_number_grid_at_fwhm",
        "discovery_resolution_fwhm_meV": float(fwhm_meV),
        **dict(provenance),
    }
    return ScanProposal(
        h=float(intent.hkl[0]), k=float(intent.hkl[1]), l=float(intent.hkl[2]),
        e_lo=e_lo, e_hi=e_hi, e_step=step, ncount=int(ncount),
        kind=DISCOVERY_SCAN_KIND, reason=reason, e_pred=float(centre),
        fwhm_dE=float(fwhm_meV), n_points=int(n_points), intent=intent, meta=meta)
