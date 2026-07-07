"""Read ISAR's file contract back (WORKING — LLM_HARNESS_DESIGN §4).

ISAR writes, once per ``isar run``, a stable set of files the driver consumes. These readers parse
them with the stdlib only and never import ISAR internals:

- :func:`read_results_csv` — ``report/results.csv`` (one row per scan×detector). Numeric columns the
  acquisition engine and stopping criterion need (``omega0``, ``interval_lo``/``interval_hi``,
  ``confidence``, ``omega0_err``, the Q components) are coerced to floats; everything else stays a
  string. The columns are ISAR's ``driver._CSV_COLUMNS``.
- :func:`read_report_json` — ``report/report.json`` (a light run summary, cheap to poll for progress).
- :func:`read_card_state` — ``card.state.json`` (the accumulated ``dispersion_prior`` +
  ``resolution_versions``; the running physics model the ω(q) model mirrors).

**Guard rail (LLM_HARNESS_DESIGN §4 WARNING).** ``prior_sigma`` is read as an opaque value only; it
is NOT a measured uncertainty (it floors at 0.3 meV by design). The convergence test consumes
``interval_lo``/``interval_hi``, never ``prior_sigma`` — enforced in
:mod:`isar.drive.acquisition.calibration`.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

# Columns coerced to float when present and non-empty. Everything else stays a string (or None).
_NUMERIC_COLUMNS = frozenset({
    "T", "h", "k", "l", "confidence", "omega0", "omega0_err",
    "interval_lo", "interval_hi", "omega0_profile_lo", "omega0_profile_hi",
    "gamma", "cost_per_dof",
    "zero", "gauss_fwhm", "lorentz_fwhm", "prior_omega", "prior_sigma",
    "secondary_omega0", "secondary_omega0_err",
})
_INT_COLUMNS = frozenset({"scan", "prior_n", "n_modes", "n_members"})
# Coerced to bool ("True"/"False" → True/False; "" → None). ISAR writes Python bool reprs.
_BOOL_COLUMNS = frozenset({"interval_reliable", "pooled"})


def _report_dir(workspace: str | Path) -> Path:
    return Path(workspace) / "report"


def _coerce(column: str, value: str | None):
    if value is None or value == "":
        return None
    if column in _BOOL_COLUMNS:
        s = str(value).strip().lower()
        if s in ("true", "1"):
            return True
        if s in ("false", "0"):
            return False
        return value
    if column in _INT_COLUMNS:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return value
    if column in _NUMERIC_COLUMNS:
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    return value


def read_results_csv(workspace: str | Path) -> list[dict[str, Any]]:
    """Parse ``<workspace>/report/results.csv`` into a list of row dicts with numeric coercion.

    Raises :class:`FileNotFoundError` if the run has not produced a ``results.csv`` yet.
    """
    path = _report_dir(workspace) / "results.csv"
    if not path.exists():
        raise FileNotFoundError(f"no results.csv at {path} (run ISAR first)")
    rows: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            rows.append({k: _coerce(k, v) for k, v in raw.items()})
    return rows


def read_report_json(workspace: str | Path) -> dict[str, Any]:
    """Parse ``<workspace>/report/report.json`` (the light run summary)."""
    path = _report_dir(workspace) / "report.json"
    if not path.exists():
        raise FileNotFoundError(f"no report.json at {path} (run ISAR first)")
    return json.loads(path.read_text(encoding="utf-8"))


def read_card_state(workspace: str | Path) -> dict[str, Any]:
    """Parse ``<workspace>/card.state.json`` — the accumulated experiment state.

    Returns the raw dict; ``dispersion_prior`` is a list of PriorPoint dicts and ``resolution_versions``
    a list of version dicts. A missing or empty (``{}``) state file yields ``{}`` — a fresh workspace
    is not an error.
    """
    path = Path(workspace) / "card.state.json"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def dispersion_prior(workspace: str | Path) -> list[dict[str, Any]]:
    """The ``dispersion_prior`` list from ``card.state.json`` (empty on a fresh workspace)."""
    return list(read_card_state(workspace).get("dispersion_prior") or [])
