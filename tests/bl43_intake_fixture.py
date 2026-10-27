"""The S1 BL43LXU test workspace plus an intake layer, generated at test time (hard rule 10).

A top-level helper beside ``tests/bl43_fixture.py`` and imported the same way. ``build_intake_workspace``
writes a resolution-curve library into ``incoming/rf`` (found beside the data at ingest: source
``"discovered"``), or none for the no-curves variant, then builds the S1 workspace with ``intake=True``,
so the import record and the curve check are written as ``isar ingest`` writes them. With ``fits=True``
it also writes one complete fast-pass run over the stamped fits through C-fit-inputs' own writers, each
line's inputs built by C-effective as the run builds them, so a fresh fixture reads as current.

Nothing here hand-writes a backend file: user entries and settings go through the intake writers
(``isar.intake.values``, ``isar.intake.settings``) in the tests themselves.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from bl43_fixture import ANALYZERS, KEY_A, build_bl43_workspace
from intake_fixtures import write_library

FITTED = (f"{KEY_A}:a03", f"{KEY_A}:a04", f"{KEY_A}:a06")     # the units build_bl43_workspace stamps


def analyzer_ids(n_analyzers: int = 4) -> tuple[str, ...]:
    return tuple(f"a{n:02d}" for n in ANALYZERS[:n_analyzers])


def build_intake_workspace(root: Path, n_analyzers: int = 4, *, curves: bool = True, fits: bool = True,
                           with_t: bool = True, off_integer=()) -> Path:
    """The S1 workspace under ``root`` with its import record and curve check; returns ``root/ws``.
    ``curves=False`` writes no library (the resolution curves are then not found)."""
    root = Path(root)
    if curves:
        write_library(root / "incoming" / "rf", analyzers=analyzer_ids(n_analyzers))
    ws_root = build_bl43_workspace(root, n_analyzers, fits=fits, intake=True, with_t=with_t,
                                   off_integer=off_integer)
    if fits:
        write_run(ws_root, FITTED)
    return ws_root


def write_run(ws_root: Path, units: Sequence[str], *, passes: Sequence[str] = ("fast",),
              end: bool = True) -> str:
    """One run over ``units`` through C-fit-inputs' writers: start, started, finished and (``end``) the
    end line ``"complete"``. Without ``end`` the run reads as running while this process lives, and as
    ended without completing once it has gone (a killed run). Returns the run id."""
    from isar.intake import fit_inputs, inputs, scans, store
    rows = scans.read_scans(ws_root)
    read = store.read_all(ws_root, store.ENTRIES)
    index = inputs.index_entries(read.entries, lines=read.lines)
    started, finished = [], []
    for unit in units:
        scan, _, det = unit.rpartition(":")
        row = rows[scan]
        u = inputs.unit_inputs(row, inputs.scan_inputs(row, index), det, None)
        kept = list(inputs.fallbacks(u))
        for p in passes:
            started.append({"unit": unit, "scan": scan, "pass": p, "inputs": u, "hash": inputs.inputs_hash(u),
                            "fallbacks": kept})
            finished.append({"unit": unit, "scan": scan, "pass": p, "status": "fitted", "fallbacks": kept,
                             "result": None})
    run = fit_inputs.begin_run(ws_root, passes=list(passes))
    fit_inputs.write_started(ws_root, run, started)
    fit_inputs.write_finished(ws_root, run, finished)
    if end:
        fit_inputs.end_run(ws_root, run, "complete")
    return run
