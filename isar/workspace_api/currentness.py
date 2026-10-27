"""Which analyzer files have a current fast or deep result (C-current), read-only and Qt-free.

The predicate is the engine's own (:mod:`isar.session.currentness`), the one a queued item applies at its
start, evaluated here on what is stored instead of a fresh parse. ``current_passes`` does file I/O
proportional to the ledger: call it from a worker thread, never the GUI thread. The fitting stack is
imported on its first call, not here.
"""
from __future__ import annotations

from isar.session.currentness import current_passes, deep_calibration_note, result_warning

__all__ = ["current_passes", "deep_calibration_note", "result_warning"]
