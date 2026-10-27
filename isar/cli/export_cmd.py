"""`isar export --workspace DIR`: write a timestamped export of the stored results (no refit).

Writes ``report/exports/export-<YYYYMMDD-HHMMSS>[-N]/`` (``isar.report.export``) and prints one
progress line per spectrum (``export: <i>/<n> spectra``), a summary line and the folder's path.
With ``--json`` the last stdout line is ``{"export": {...}}``, or ``{"error": {...}}`` on failure.
Exit 0 on success (per-spectrum problems included), 2 when DIR is not a workspace, 1 otherwise.
Current contract: docs/WORKSPACE_AND_CLI.md.
"""
from __future__ import annotations

import json
import logging
import sys

from isar.report.export import export_workspace
from isar.session.workspace import Workspace

log = logging.getLogger(__name__)


def _fail(kind: str, message: str, as_json: bool, code: int) -> int:
    if as_json:
        print(json.dumps({"error": {"kind": kind, "message": message}}), flush=True)
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def _progress(i: int, n: int) -> None:
    print(f"export: {i}/{n} spectra", flush=True)


def run(args) -> int:
    as_json = getattr(args, "json", False)
    ws = Workspace.open(args.workspace)
    if not ws.exists():
        return _fail("no_workspace", f"no workspace at {ws.root} (run `isar init` first)", as_json, 2)
    try:
        s = export_workspace(ws.root, progress=_progress)
    except Exception as exc:  # noqa: BLE001 — logged with its traceback, then reported
        log.exception("export failed")
        return _fail("export_failed", f"{type(exc).__name__}: {exc}", as_json, 1)
    folder = str(s.folder.resolve())
    print(f"export: {s.n_spectra} spectra, {s.n_modes} mode rows ({s.n_modes_fast} fast, "
          f"{s.n_modes_deep} deep), {s.n_curves} resolution curves, {len(s.problems)} problems "
          f"(listed in fits.json)")
    print(folder)
    if as_json:
        print(json.dumps({"export": {
            "folder": folder, "spectra": s.n_spectra, "modes": s.n_modes,
            "modes_fast": s.n_modes_fast, "modes_deep": s.n_modes_deep,
            "resolution_curves": s.n_curves, "problems": len(s.problems)}}))
    sys.stdout.flush()
    return 0
