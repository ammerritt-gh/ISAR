"""Which analyzer files have a current fast or deep result (C-current), read-only and Qt-free.

The predicate is the engine's own (:mod:`isar.session.currentness`), the one a queued item applies at its
start, here evaluated on what is stored instead of a fresh parse. File I/O proportional to the ledger: call
it from a worker thread, never the GUI thread. The fitting stack is imported on the first call, not here.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from isar.session.currentness import deep_calibration_note, result_warning

__all__ = ["current_passes", "deep_calibration_note", "result_warning"]

log = logging.getLogger(__name__)


def current_passes(workspace_root: Path | str, unit_keys: Iterable[str] | None = None) -> dict[str, frozenset[str]]:
    """``unit_key -> {"fast", "deep"} subset`` with a current stored result, for ``unit_keys`` (None: every
    analyzer file in the ledger); an unknown key gives ``frozenset()``. Reads the workspace once and never
    writes; ``FileNotFoundError`` when there is no workspace at ``workspace_root``."""
    from isar.intake import curves, inputs, scans as intake_scans, store
    from isar.session import calibrations, currentness, guidelines as guide_mod
    from isar.session.workspace import Workspace

    card, session = Workspace.open(workspace_root).load()
    root = Path(workspace_root)
    keys = list(session.scans) if unit_keys is None else list(unit_keys)
    g = guide_mod.resolve(card)
    runcal = calibrations.calibration_view(card)
    request = currentness.queue_deep_request()
    bl43 = intake_scans.applies(card)
    rows, index, library_fp = {}, None, None
    if bl43:
        rows = intake_scans.read_scans(root)
        read = store.read_all(root, store.ENTRIES)
        index = inputs.index_entries(read.entries, lines=read.lines)
        folder, pattern, _source = curves.resolution_location(card, root)
        if folder is not None and pattern:
            from isar.parsers import sarf
            try:
                library_fp = sarf.fingerprint(folder, pattern)
            except (OSError, ValueError) as exc:
                log.warning("current_passes: the curve library at %s is unreadable (%s)", folder, exc)
    inputs_of: dict = {}
    out: dict[str, frozenset[str]] = {}
    for key in keys:
        prev = session.get(key)
        if prev is None:
            out[key] = frozenset()
            continue
        det = key.rsplit(":", 1)[1]
        row = si = None
        if bl43:
            scan = key.rsplit(":", 1)[0]
            row = rows.get(scan)
            if row is None:              # a BL43 unit with no import row has nothing to compare with
                out[key] = frozenset()
                continue
            si = inputs_of.get(scan) or inputs_of.setdefault(scan, inputs.scan_inputs(row, index))
        now = currentness.stored_now(prev, det, g=g, runcal=runcal, row=row, scan_inputs=si, library_fp=library_fp)
        out[key] = currentness.judge(prev, det, now, request)
    return out
