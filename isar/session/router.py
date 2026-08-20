"""Event-stream routing (UNIFICATION_DESIGN §2.4, docs/ARCHITECTURE.md §4).

Every scan is a measurement or a context-update. `route` partitions a batch on `record.scan_type`
(set by the parser — classification never leaks downstream). `apply_context_updates` consumes the
context events into session state *before* any measurement is fitted: calibration events refresh
the energy-zero/resolution state (delegated to `calibrations`, §3), alignment events append to the
sample map, environment events are noted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from isar.schema.card import Card
from isar.session.state import SessionState


@dataclass
class RoutedBatch:
    measurements: list = field(default_factory=list)
    calibrations: list = field(default_factory=list)
    alignments: list = field(default_factory=list)
    environments: list = field(default_factory=list)
    other: list = field(default_factory=list)   # unknown scan_type — surfaced, never silently fit


def route(records) -> RoutedBatch:
    rb = RoutedBatch()
    bucket = {
        "measurement": rb.measurements,
        "calibration": rb.calibrations,
        "alignment": rb.alignments,
        "environment": rb.environments,
    }
    for rec in records:
        bucket.get(rec.scan_type, rb.other).append(rec)
    return rb


def _sample_of(record, card: Card):
    """Best-effort sample label for an alignment event (motor name / experiment id fallback)."""
    name = (card.policy.sample or {}).get("name")
    return name or record.experiment_id or None


def apply_context_updates(routed: RoutedBatch, card: Card, session: SessionState,
                          *, companion_files=None) -> list[str]:
    """Fold context events into `card.state`. Returns human-readable update lines for the log/report.

    Calibration handling (energy-zero series + resolution versions) is delegated to
    `isar.session.calibrations.refresh` (§3); this function wires alignment and environment updates
    and calls calibrations when it is available.
    """
    lines: list[str] = []

    # --- calibration: delegate to the calibration layer (§3) --------------------
    if routed.calibrations or companion_files:
        try:
            from isar.session import calibrations
        except ImportError:
            calibrations = None
        if calibrations is not None:
            lines.extend(calibrations.refresh(
                card, session,
                calibration_records=routed.calibrations,
                companion_files=list(companion_files or []),
            ) or [])

    # --- alignment: record the current sample (§4, P4) -------------------------
    for rec in routed.alignments:
        sample = _sample_of(rec, card)
        card.state.sample_map.append(
            {"from_scan": rec.scan_number, "sample": sample, "source": "alignment"}
        )
        lines.append(f"alignment scan {rec.scan_number}: sample -> {sample!r}")

    # --- environment: note only (v1) -------------------------------------------
    for rec in routed.environments:
        lines.append(f"environment scan {rec.scan_number}: noted")

    return lines
