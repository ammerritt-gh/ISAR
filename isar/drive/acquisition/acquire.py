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
* :func:`normalize_proposal_score` / :func:`hkl_key` — score bookkeeping and the suppression key.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable

from isar.drive.acquisition.eig import counts_eig, eig_per_cost, predicted_sigma_after
from isar.drive.acquisition.inventory import MeasurementIntent
from isar.drive.acquisition.scan_design import DEFAULT_FWHM_MEV
from isar.drive.acquisition.scan_grid import format_scan_command

_LOG = logging.getLogger(__name__)


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
    # In-memory only: refresh exposure-sensitive score fields after runtime clamps. Deliberately
    # omitted from to_dict(), so the event/file contract remains plain JSON. Nothing on the EIG path
    # sets it; it survives because the retired scan-window comparison still constructs proposals
    # with it, and that stack is deleted in its own slice.
    score_recompute: Callable[[int], dict[str, float | None]] | None = field(
        default=None, repr=False, compare=False)
    # In-memory only: the scientific question this request realizes. The JSON contract carries only
    # ``meta["scientific_action_fingerprint"]``; the object itself is what lets the loop build the
    # finalized stage against the intent it belongs to instead of against a bare hash string.
    intent: MeasurementIntent | None = field(default=None, repr=False, compare=False)
    # In-memory only, and nothing on the EIG path touches these three: they survive because the
    # retired scan-window comparison still constructs proposals with them, and that whole stack
    # (eig_runtime / eig_shadow / realized_comparison / authority) is deleted in its own slice.
    scan_design_inventory: Any | None = field(default=None, repr=False, compare=False)
    shadow_scoring_source: Any | None = field(default=None, repr=False, compare=False)
    realized_scoring_remaining_seconds: float | None = field(
        default=None, repr=False, compare=False)

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


def normalize_proposal_score(proposal: ScanProposal, tolerance: float) -> None:
    """Make score diagnostics describe the proposal's current emitted point/count batch."""
    score = (proposal.meta or {}).get("score")
    if not isinstance(score, dict):
        return
    dc = float(proposal.n_points) * float(proposal.ncount)
    method = score.get("method")
    sigma_prior = score.get("sigma_prior")
    c_spent = float(score.get("c_spent") or 0.0)
    if method in ("counts", "deficit") and sigma_prior is not None and c_spent > 0.0:
        eig = counts_eig(float(sigma_prior), float(tolerance), c_spent, dc)
        sigma_scan = predicted_sigma_after(float(sigma_prior), c_spent, dc)
        classification = float(score.get("classification_eig") or 0.0)
        combined = eig + float(score.get("classification_weight") or 0.0) * classification
        score.update({"eig": _finite_or_none(eig),
                      "sigma_scan": _finite_or_none(sigma_scan),
                      "combined_information": _finite_or_none(combined)})
    information = score.get("combined_information")
    if information is None:
        information = score.get("eig")
    score["cost"] = dc
    score["eig_per_cost"] = (None if information is None
                             else _finite_or_none(eig_per_cost(float(information), dc)))


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


def _finite_or_none(x) -> float | None:
    """``x`` if finite, else ``None`` — so ``+inf`` scores round-trip through strict JSON."""
    return float(x) if x is not None and math.isfinite(x) else None


def hkl_key(hkl) -> str:
    """The stable string key an unreachable-q suppression is recorded under."""
    return ",".join(f"{round(float(value), 6):g}" for value in hkl)
