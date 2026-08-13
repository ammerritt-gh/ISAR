"""Durable append log of measurement-arena records (SCENE-4 PR 4.0, work package #26).

One line per :meth:`isar.schema.measurement.MeasurementArena.to_dict()`, appended to
``<workspace>/report/measurements.jsonl``. Same fsync-append idiom as
:func:`isar.scene.store._append_history`: durable per line, written whole, flushed and fsynced —
a crash mid-write leaves every earlier line intact.

Last record per ``scan_ref`` wins on read. The reader lives in
:mod:`isar.workspace_api.scene` (not here): ``isar.workspace_api`` may not import
:mod:`isar.session` (hard rule 6), so it reads this file directly through
:class:`isar.schema.measurement.MeasurementArena`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

FILENAME = "measurements.jsonl"


def append_measurement(report_dir, record: dict) -> None:
    """Append one measurement-arena record (already ``.to_dict()``-shaped) to the log.

    ``report_dir`` is created if missing — a workspace's first delivered scan is the first write.
    Called once per arena, BEFORE any fit is attempted for it (the producer is
    :mod:`isar.session.scan_delivery`), so this line survives a ``fit_one`` crash on the same
    arena untouched.
    """
    root = Path(report_dir)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / FILENAME, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
