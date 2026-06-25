"""The format-agnostic report model (§7.4): pure data the HTML/Markdown renderers share.

The heart is the **removal ledger** — for every reduced spectrum, one entry per rejected miniscan
pass and per masked point, each with a stable id, the magnitude removed, the rule that decided it,
and the reversibility note. "Masked points shown, not hidden": the model records what the pipeline
dropped and why, never the act of deleting it (nothing is deleted, §0.4). Everything here is plain
str/int/float/list/dict so the model serializes straight to the JSON sidecar the §8 review loop will
consume; the heavy arrays stay on the records and are handed to `figures` separately.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from isar.schema import axes

_REVERSIBLE = "raw retained — reversible (mask, never deletion; §0.4)"

# Stable figure ids — the shared convention between `figures.report_figures` and the renderers.
FIG_COVERAGE = "coverage"


def fig_spectrum(scan_number: int, detector_id: str) -> str:
    return f"spectrum:{scan_number}:{detector_id}"


def fig_miniscan(scan_number: int, detector_id: str, point: int) -> str:
    return f"miniscan:{scan_number}:{detector_id}:p{point}"


@dataclass
class Removal:
    stable_id: str               # exp:scan:det:p{point}  — addressable by the §8 review surface
    kind: str                    # "cosmic_ray_rejected" | "masked"
    scan_number: int
    detector_id: str
    point: int
    energy: float | None
    passes: list                 # rejected miniscan-pass indices (empty for a masked point)
    magnitude: float             # counts removed (0 for a pure mask)
    rule: str | None             # "poisson" | "sigma_clip" | None
    reason: str
    reversible: str = _REVERSIBLE


@dataclass
class SpectrumSection:
    detector_id: str
    q: list
    q_source: str
    n_points: int
    n_masked: int
    reject_source: str | None
    has_miniscans: bool
    removals: list = field(default_factory=list)


@dataclass
class ScanSection:
    scan_number: int
    scan_type: str
    temperature: float
    energy_min: float | None
    energy_max: float | None
    spectra: list = field(default_factory=list)
    narrative: str | None = None    # §7.3 LLM slot — empty in this deterministic chunk
    decision: str | None = None     # §8 slot


@dataclass
class ReportModel:
    experiment_id: str
    facility: str
    instrument: str
    plugin: str
    plugin_version: str
    format_version: str
    schema_version: str
    rule: str
    energy_status: str
    n_scans: int
    n_measurements: int
    summary: dict
    catalogue: list
    coverage: dict
    rollup: dict
    scans: list
    other_scans: list
    note: str = ("Signal is RAW COUNTS (errors √N); the monitor is preserved for optional, reversible "
                 "normalization, never baked in. Masked/rejected points are shown, never hidden, and "
                 "never deleted — the raw arrays remain, so every decision is reversible (§0.4, §7.4).")


def _energy(record):
    scanned = axes.scanned_axes(record)
    return scanned[0].values if scanned else None


def _spectrum_section(record, det_id, sp) -> SpectrumSection:
    energy = _energy(record)
    removals: list = []
    exp, scan = record.experiment_id, record.scan_number

    # cosmic-ray rejections: read the per-pass truth straight from the ensemble (not the reason text)
    if sp.miniscan_reject is not None and sp.miniscan_counts is not None:
        rejected = sp.miniscan_reject & ~np.isnan(sp.miniscan_counts)
        for i in np.nonzero(rejected.any(axis=1))[0]:
            passes = [int(m) for m in np.nonzero(rejected[i])[0]]
            magnitude = float(np.nansum(np.where(rejected[i], sp.miniscan_counts[i], 0.0)))
            removals.append(Removal(
                stable_id=f"{exp}:{scan}:{det_id}:p{int(i)}", kind="cosmic_ray_rejected",
                scan_number=scan, detector_id=det_id, point=int(i),
                energy=(float(energy[i]) if energy is not None else None),
                passes=passes, magnitude=magnitude, rule=sp.miniscan_reject_source,
                reason=(f"{len(passes)} of {int(np.sum(~np.isnan(sp.miniscan_counts[i])))} passes "
                        f"rejected as cosmic ray(s) ({sp.miniscan_reject_source})")))

    # masked points: normalization undefined (zero kept monitor); kept in the array, flagged here
    cosmic_points = {rm.point for rm in removals}
    for i in np.nonzero(~sp.mask.astype(bool))[0]:
        if int(i) in cosmic_points:
            continue
        removals.append(Removal(
            stable_id=f"{exp}:{scan}:{det_id}:p{int(i)}", kind="masked",
            scan_number=scan, detector_id=det_id, point=int(i),
            energy=(float(energy[i]) if energy is not None else None),
            passes=[], magnitude=0.0, rule=None,
            reason="masked: zero kept monitor — normalization undefined at this point"))

    removals.sort(key=lambda rm: rm.point)
    return SpectrumSection(
        detector_id=det_id, q=[round(float(v), 5) for v in sp.q], q_source=sp.q_source,
        n_points=int(len(sp.intensity)), n_masked=int(np.sum(~sp.mask.astype(bool))),
        reject_source=sp.miniscan_reject_source,
        has_miniscans=sp.miniscan_counts is not None, removals=removals)


def _scan_section(record) -> ScanSection:
    energy = _energy(record)
    spectra = [_spectrum_section(record, det_id, sp)
               for det_id, sp in record.spectra.items()]
    return ScanSection(
        scan_number=record.scan_number, scan_type=record.scan_type,
        temperature=(float(record.temperature.setpoint[0])
                     if len(record.temperature.setpoint) else float("nan")),
        energy_min=(float(np.min(energy)) if energy is not None and len(energy) else None),
        energy_max=(float(np.max(energy)) if energy is not None and len(energy) else None),
        spectra=spectra)


def _catalogue(collation) -> list:
    return [{"key_fields": g.key_fields, "n_members": len(g.members),
             "members": [[m.scan_number, m.detector_id] for m in g.members],
             "merge": g.merge}
            for g in collation.groups]


def build_report_model(records, collation, card) -> ReportModel:
    """Assemble the report model from reduced records + their collation. Read-only over the records."""
    measurements = [r for r in records if r.scan_type == "measurement"]
    others = [r for r in records if r.scan_type != "measurement"]
    head = (records[0] if records else None)
    src = head.source if head else None
    cov = collation.coverage
    roll = collation.rollup

    scans = [_scan_section(r) for r in measurements]
    n_removals = sum(len(sp.removals) for s in scans for sp in s.spectra)

    return ReportModel(
        experiment_id=(src.file if src else ""),
        facility=card.policy.facility, instrument=card.policy.instrument,
        plugin=(src.plugin if src else card.policy.plugin),
        plugin_version=(src.plugin_version if src else ""),
        format_version=(src.format_version if src else card.policy.format_version),
        schema_version=(src.schema_version if src else ""),
        rule=(card.policy.reduction or {}).get("rule", ""),
        energy_status=(head.energy_calibration.status if head else ""),
        n_scans=len(records), n_measurements=len(measurements),
        summary={"removals": n_removals, "passes_rejected": roll.passes_rejected,
                 "points_with_rejection": roll.points_with_rejection,
                 "counts_removed": round(roll.counts_removed, 3),
                 "groups": len(collation.groups)},
        catalogue=_catalogue(collation),
        coverage={"component": cov.component, "values": cov.values, "gaps": cov.gaps},
        rollup={"point_flags": roll.point_flags, "spectrum_flags": roll.spectrum_flags,
                "scan_flags": roll.scan_flags, "passes_rejected": roll.passes_rejected,
                "points_with_rejection": roll.points_with_rejection,
                "counts_removed": round(roll.counts_removed, 3)},
        scans=scans,
        other_scans=[{"scan_number": r.scan_number, "scan_type": r.scan_type,
                      "axis": (axes.scanned_axes(r)[0].name if axes.scanned_axes(r) else "")}
                     for r in others])
