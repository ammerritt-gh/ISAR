"""The ISAR invoker (LLM_HARNESS_DESIGN §2.3, §4) — drive ISAR by files + a subprocess.

ISAR exposes no query API; its stable interface is its files. This package is the whole seam:

- :mod:`scan_writer` — save a finished TAVI scan verbatim into the workspace data dir as a JSON the
  ``tavi`` parser plugin sniffs (top-level ``job_id`` + ``result``).
- :mod:`runner` — run ``isar run --workspace <dir>`` as a subprocess (idempotent; re-fits only new /
  changed scans), raising on a nonzero exit.
- :mod:`readers` — read the file contract back: ``report/results.csv``, ``report/report.json``,
  ``card.state.json`` (``dispersion_prior``).

Nothing here imports ISAR analysis internals — the subprocess IS the boundary (see the package
docstring for :mod:`isar.drive`).
"""
from __future__ import annotations

from isar.drive.isar_io.readers import read_card_state, read_report_json, read_results_csv
from isar.drive.isar_io.runner import run_isar
from isar.drive.isar_io.scan_writer import save_scan_json

__all__ = ["save_scan_json", "run_isar", "read_results_csv", "read_report_json", "read_card_state"]
