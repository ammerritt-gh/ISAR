"""The scan request the instrument client submits, and the resolution it is sized against.

This module used to *choose* scans. It no longer does: the one place a Q, an energy window or an
exposure is chosen is :func:`isar.drive.acquisition.eig_planner.plan_next`, called at the single
seam in :func:`isar.drive.loop.run_campaign` (ruling 2026-09-02 §6). The legislated placement rules
that lived here — the selection tiers, the tracked frontier/anchor/bridge picks, the same-q
ceilings, the ``survey`` flag, the generator registry and the action-inventory plan — are deleted,
not routed around.

What remains is the value type and the two pure helpers the loop and the discovery lane still call:

* :class:`ScanProposal` — a complete constant-Q energy scan, ready to validate and submit;
* :func:`_resolve_fwhm` / :func:`_fwhm_from` — the energy-resolution FWHM a window is sized against;
* :func:`hkl_key` — the key an unreachable-q suppression is recorded under;
* the three refusals the loop turns into a rejection class when a design cannot be realized.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from isar.drive.acquisition.inventory import MeasurementIntent
from isar.drive.acquisition.scan_grid import format_scan_command

_LOG = logging.getLogger(__name__)

#: Energy-resolution FWHM (meV) assumed when the instrument reports none.
DEFAULT_FWHM_MEV = 0.5


class BudgetInsufficientForMinimumBatch(ValueError):
    """The remaining neutron budget cannot fund one policy-floor scan."""


class InstrumentLimitBelowMinimumBatch(ValueError):
    """The instrument cannot accept one policy-floor scan."""


class InstrumentPointLimitExceeded(ValueError):
    """The emitted request still exceeds the live instrument point ceiling."""


@dataclass
class ScanProposal:
    """A complete constant-Q energy-scan request the instrument client can submit."""

    h: float
    k: float
    l: float
    e_lo: float
    e_hi: float
    e_step: float
    ncount: int
    kind: str                # "quicklook" | "production" | "confirm"
    reason: str
    e_pred: float = 0.0
    fwhm_dE: float = 0.0
    n_points: int = 0
    acquisition_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # In-memory only: the scientific question this request realizes. The JSON contract carries only
    # ``meta["scientific_action_fingerprint"]``; the object itself is what lets the loop build the
    # finalized stage against the intent it belongs to instead of against a bare hash string.
    intent: MeasurementIntent | None = field(default=None, repr=False, compare=False)

    def scan_command(self) -> str:
        """The TAVI ``scan_command1`` for this window: ``deltaE <lo> <hi> <step>``.

        Rendered by the shared grid helper, which is also what candidate construction formats — one
        rendering, so a designed window and the submitted request cannot drift apart.
        """
        return format_scan_command(self.e_lo, self.e_hi, self.e_step)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hkl": [self.h, self.k, self.l], "e_lo": self.e_lo, "e_hi": self.e_hi,
            "e_step": self.e_step, "ncount": self.ncount, "kind": self.kind, "reason": self.reason,
            "e_pred": self.e_pred, "fwhm_dE": self.fwhm_dE, "n_points": self.n_points,
            "acquisition_id": self.acquisition_id,
            "scan_command": self.scan_command(), "meta": dict(self.meta),
        }


def _resolve_fwhm(resolution_info, h: float, k: float, l: float, deltaE: float) -> float:
    """Extract the energy-resolution FWHM (meV) from ``resolution_info``.

    Accepts a plain float, a dict (``{"fwhm_dE": x}`` or a raw ``GET /resolution`` body with
    ``fwhm.dE``, honoring ``ok == false`` refusals), or a callable ``(h,k,l,deltaE) -> (any of these)``.
    Falls back to :data:`DEFAULT_FWHM_MEV` when nothing usable is present.
    """
    info = resolution_info(h, k, l, deltaE) if callable(resolution_info) else resolution_info
    return _fwhm_from(info)


def _fwhm_from(info) -> float:
    """The energy FWHM to size scan windows and steps with.

    ``GET /resolution`` reports two energy widths of the same ellipsoid, and they differ by a
    factor of ~2.5 on the TAVI configuration this drives:

    - ``fwhm.dE`` is the MARGINAL width, ``sqrt(8 ln2 * inv(M)[E,E])`` — the ellipsoid projected
      onto the energy axis, integrating over Q. TAVI publishes the identical number as
      ``vanadium_fwhm_meV``, because that projection is exactly what an incoherent elastic
      scatterer measures.
    - ``bragg.dE`` is the SECTION width, ``sqrt(8 ln2 / M[E,E])`` — the ellipsoid cut at fixed Q.

    A constant-Q energy scan through a sharp excitation measures a section, not a projection, so
    the marginal width overestimates the peak it will actually see (a projection is never narrower
    than the corresponding section). Sizing windows on it made every target core ~2.5x too wide,
    which is what overflowed the declared energy domain at high q. Prefer the section.

    The exact width of a DISPERSIVE mode is a slope-dependent section — focusing or defocusing with
    the sign of the dispersion against the ellipsoid tilt — and lies between these two; TAVI exposes
    ``projections`` for that refinement. ``bragg.dE`` is the right order and the honest choice until
    that is built.
    """
    if info is None:
        return DEFAULT_FWHM_MEV
    if isinstance(info, (int, float)):
        val = float(info)
        return val if val > 0 else DEFAULT_FWHM_MEV
    if isinstance(info, dict):
        if info.get("ok") is False:               # /resolution refusal (infeasible geometry)
            return DEFAULT_FWHM_MEV
        bragg = info.get("bragg")
        if isinstance(bragg, dict) and bragg.get("dE"):
            value = float(bragg["dE"])
            if value > 0:
                return value
        if "fwhm_dE" in info and info["fwhm_dE"]:
            return float(info["fwhm_dE"])
        fwhm = info.get("fwhm")
        if isinstance(fwhm, dict) and fwhm.get("dE"):
            return float(fwhm["dE"])
        if isinstance(fwhm, (int, float)) and fwhm > 0:
            return float(fwhm)
    return DEFAULT_FWHM_MEV


def hkl_key(hkl) -> str:
    """The stable string key an unreachable-q suppression is recorded under."""
    return ",".join(f"{round(float(value), 6):g}" for value in hkl)
