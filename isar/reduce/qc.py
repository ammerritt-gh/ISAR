"""Run the deterministic QC layer over a record (§7.1-7.2) — the single call between reduce and collate.

`qc_record(record, card)` runs the point-level checks first (qc_points masks the leading stale run)
then the scan/spectrum-level checks (qc_scans: truncated / empty / dead_detector / sensor_divergence).
Both mutate the record in place and never raise (§0.5: a QC failure is a benign over-flag or a
"couldn't check", never a crashed batch). Returns the record.
"""
from __future__ import annotations

from isar.reduce import qc_points as _qc_points
from isar.reduce import qc_scans as _qc_scans


def qc_record(record, card):
    _qc_points.qc_points(record, card)
    _qc_scans.qc_scans(record, card)
    return record
