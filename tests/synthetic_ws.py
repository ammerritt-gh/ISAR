"""Synthetic-workspace builders shared across the facade and GUI test suites.

Writes the minimal on-disk artifacts a real workspace has (card.policy.toml / card.state.json /
session.json / report/results.csv / report/session_log.jsonl / data/*.json /
report/campaign_summary.json) so the facade (and the GUI, which reads only through the facade) can
be exercised without a full campaign.

Factored out of tests/workspace_api/conftest.py so the GUI tests import the same builder rather than
copying it. Importable as a top-level module: pytest puts tests/ on sys.path (workspace_api is a
package under it), so ``from synthetic_ws import stamp_ws`` resolves from any conftest.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

_POLICY_TOML = """\
facility = "TAVI"
instrument = "PUMA"
format_version = "v1"
plugin = "tavi"
energy_column = "deltaE"
monitor_column = "number_neutrons"
counting_mode = "monitor"
normalize_by = "monitor"
raw_abscissa_kind = "energy"
energy_status = "absolute"
measurement_pattern = "deltaE"
alignment_pattern = "omega"
min_points = 5
hkl_round_step = 0.05

[temperature_rule]
setpoint = 300.0

[collation]
key = ["sample", "q", "T"]
merge = "separate"
t_round = 5.0

[[detectors]]
detector_id = "det"
value_index = 0
signal_column = "counts"
enabled = true
"""

# A superset of the interval-ladder columns the facade parses. Tests pass a subset of keys per row;
# absent keys are written as empty cells (or the column omitted entirely for old-format tests).
FULL_COLUMNS = [
    "scan", "detector", "sample", "T", "h", "k", "l", "status", "verdict", "disposition",
    "confidence", "omega0", "omega0_err", "interval_lo", "interval_hi", "interval_reliable",
    "omega0_profile_lo", "omega0_profile_hi", "hesse_lo", "hesse_hi", "hesse_trusted",
    "bayes_lo", "bayes_med", "bayes_hi", "bayes_ess", "log_evidence",
    "pooled", "n_members", "members", "gamma", "gamma_provisional", "n_modes",
    "best_model", "cost_per_dof", "fit_kind", "branch_id", "resolvable",
    "zero", "gauss_fwhm", "lorentz_fwhm", "reasons", "amplitudes", "candidates",
    "scan_id",
]

# The old MVP-16-era header: no bayes / members / amplitudes / candidates columns.
OLD_COLUMNS = [
    "scan", "detector", "sample", "T", "h", "k", "l", "status", "verdict", "disposition",
    "confidence", "omega0", "omega0_err", "interval_lo", "interval_hi", "interval_reliable",
    "gamma", "zero", "gauss_fwhm", "lorentz_fwhm", "reasons",
]


def write_policy(root: Path, toml_text: str = _POLICY_TOML) -> None:
    (root / "card.policy.toml").write_text(toml_text, encoding="utf-8")


def write_state(root: Path, **state) -> None:
    (root / "card.state.json").write_text(json.dumps(state), encoding="utf-8")


def write_session(root: Path, scans: dict | None = None, files: dict | None = None) -> None:
    doc = {"schema_version": "isar.session/2", "files": files or {},
           "scans": scans or {}, "data_paths": [], "last_run": {}}
    (root / "session.json").write_text(json.dumps(doc), encoding="utf-8")


def write_results(root: Path, rows: list[dict], columns: list[str] = FULL_COLUMNS) -> None:
    rd = root / "report"
    rd.mkdir(parents=True, exist_ok=True)
    with open(rd / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in columns})


def write_session_log(root: Path, lines: list[str]) -> None:
    rd = root / "report"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "session_log.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_data_file(root: Path, scan_number: int, energy, counts, mask=None) -> None:
    dd = root / "data"
    dd.mkdir(parents=True, exist_ok=True)
    doc = {"job_id": f"j-{int(scan_number):04d}", "state": "done",
           "result": {"variable_1": "deltaE",
                      "scan_values_1": list(energy), "counts": list(counts),
                      "valid_mask_1": list(mask) if mask is not None else [True] * len(energy)}}
    (dd / f"j-{int(scan_number):04d}.json").write_text(json.dumps(doc), encoding="utf-8")


def write_unit_file(root: Path, *, exp="tavi", scan_number, det="det", x, counts,
                    variance=None, mask=None, axis=None) -> Path:
    """Write a per-unit ``isar.unit_data/1`` file (``data/ingest/ingest-<exp>-<scan:04d>-<det>.json``)
    the way Phase-2 ingest does, so the facade's ``unit_data`` accessor and the inspector's unit view
    can be exercised without a real ingest. ``variance`` defaults to ``counts`` (Poisson, σ²=0 kept);
    ``axis`` defaults to an absolute-energy descriptor. Mirrors ``isar.session.unit_data.unit_data_path``."""
    from isar.session.unit_data import unit_data_path
    var = list(variance) if variance is not None else [float(c) for c in counts]
    ax = axis if axis is not None else {"name": "deltaE", "kind": "energy", "units": "meV",
                                        "raw_abscissa_kind": "energy", "calibration_status": "absolute"}
    payload = {
        "schema": "isar.unit_data/1",
        "scan_id": f"{exp}:{scan_number}:{det}",
        "experiment_id": exp, "scan_number": scan_number, "detector_id": det,
        "x": list(x), "counts": list(counts), "variance": var,
        "mask": list(mask) if mask is not None else [True] * len(counts),
        "axis": ax, "written_at": "2026-01-01T00:00:00+00:00",
    }
    path = unit_data_path(root / "data", exp, scan_number, det)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_campaign(root: Path, doc: dict) -> None:
    rd = root / "report"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "campaign_summary.json").write_text(json.dumps(doc), encoding="utf-8")


def detector_outcome(**kw) -> dict:
    """A FitOutcome-shaped detector dict for session.json (ledger side)."""
    base = {"detector_id": "det", "verdict": "resolved", "disposition": "resolved",
            "confidence": 0.9, "omega0": kw.get("omega0"), "gamma": kw.get("gamma"),
            "q": kw.get("q", [2.0, 0.0, 0.0]), "T": kw.get("T", 200.0),
            "sample": kw.get("sample", "tavi"),
            "calib": kw.get("calib", {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0})}
    base.update({k: v for k, v in kw.items() if k not in ("omega0", "gamma", "q", "T", "sample", "calib")})
    return base


def scan_entry(scan_number: int, *, scan_type="measurement", status="fitted",
               detectors=None, provenance=None) -> dict:
    sid = f"tavi:{scan_number}:det"
    return {"scan_id": sid, "scan_number": scan_number, "scan_type": scan_type,
            "status": status, "detectors": detectors or {},
            "provenance": provenance or {"M": 200, "seed": scan_number}}


def stamp_ws(root: Path, **parts) -> Path:
    """Stamp a synthetic workspace under an existing empty ``root`` directory; return ``root``."""
    write_policy(root, parts.get("policy_toml", _POLICY_TOML))
    write_state(root, **parts.get("state", {}))
    write_session(root, scans=parts.get("scans"), files=parts.get("files"))
    if "rows" in parts:
        write_results(root, parts["rows"], parts.get("columns", FULL_COLUMNS))
    if "log_lines" in parts:
        write_session_log(root, parts["log_lines"])
    for df in parts.get("data_files", []):
        write_data_file(root, **df)
    for uf in parts.get("unit_files", []):
        write_unit_file(root, **uf)
    if "campaign" in parts:
        write_campaign(root, parts["campaign"])
    return root
