"""Read-only views of the BL43 intake records: the fact rows and what each fit used (C-read-facts,
C-read-fit-inputs).

Pure functions over data ``Workspace`` has already parsed (``intake_scans.json``, ``entries.jsonl``,
``fit_inputs.jsonl``, the card state's tabulated calibration and the curve check); nothing here reads a
file or writes one. "Changed since" is judged on values: the inputs a pass used against the inputs in force
now, built by the same ``isar.intake.inputs`` functions the run uses, so the reads and the run's freshness
hash never disagree. The curves are never hashed here: the folder, library and curve identity in force are
those persisted in ``report/curves.json`` at the last import or run start. Qt- and scipy-free.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from isar.intake import inputs
from isar.intake.inputs import Change

log = logging.getLogger(__name__)

_BASIS_FALLBACK = {"inferred": "slit_inferred", "broadest_curve_fallback": "slit_broadest_curve"}


@dataclass(frozen=True)
class FactRow:
    scan: str                     # "spec1:42"
    analyzer: str | None          # None for scan facts; "a03" for Q and counts
    fact: str                     # "temperature" | "slit" | "q" | "counts"
    unit: str | None              # "K" | "mm" | None (Q in rlu as read)
    enterable: bool               # True for temperature and slit
    file_value: object | None
    file_source: str              # header|trailer|sidecar|filename|card|inferred|missing
    file_conflicts: tuple[dict, ...]   # the row fact's "conflicts", verbatim; () when none
    user_value: object | None
    user_entry: str | None
    user_reason: str | None
    used_value: object | None
    used_source: str              # "user" | "file" | "missing"
    disagreement: bool
    fallback: str | None          # "bose_t0_limit" | "slit_inferred" | "slit_broadest_curve" | "slit_pending" | None


@dataclass(frozen=True)
class PassInputs:
    pass_name: str                # "fast" | "deep"
    state: str                    # "running" | "finished"
    run: str
    started_at: str
    finished_at: str | None
    status: str | None
    inputs: Mapping               # the fit-inputs log's "inputs", as frozen at the scan's start point
    fallbacks: tuple[str, ...]
    result: Mapping | None
    changes: tuple[Change, ...]   # against the inputs in force now; () when the values are the same

    @property
    def changed(self) -> bool:
        return bool(self.changes)


@dataclass(frozen=True)
class UnitFitInputs:
    unit: str                     # "spec1:42:a03"
    scan: str
    analyzer: str
    fast: PassInputs | None       # latest counting fast pass, or None
    deep: PassInputs | None       # latest counting deep pass, or None


def _order(rows: Mapping[str, dict], scan: str) -> tuple:
    row = rows.get(scan) or {}
    n = row.get("scan_number")
    return (str(row.get("experiment_id", scan)), n if isinstance(n, int) else 0, scan)


def _slit_fallback(row: dict, si: inputs.ScanInputs, state: dict | None, check, folder_now: str | None) -> str | None:
    """Why a missing slit still has a setting: the calibration's basis while that calibration was made
    from the inputs in force now, else ``slit_pending`` (a recalibration is due)."""
    if state is None or check is None or check.folder != folder_now:
        return "slit_pending"
    dets = sorted(d for d in row.get("analyzers") or {} if d not in si.dead)
    key = inputs.calibration_inputs_key(
        data_revision=row.get("data_revision"), folder=check.folder, pattern=check.pattern, library=check.library,
        declared_slit=None,      # only asked for when no slit is in force
        analyzers=dets,
        counts_accepted=[d for d in dets if d in si.counts_accepted])
    if state.get("inputs_key") != key:
        return "slit_pending"
    return _BASIS_FALLBACK.get(state.get("setting_basis"))


def _scan_fact_row(scan: str, fact: str, fi: inputs.FactInput, row_fact: dict, fallback: str | None) -> FactRow:
    return FactRow(
        scan=scan, analyzer=None, fact=fact, unit=inputs.ENTERABLE[fact], enterable=True,
        file_value=fi.file_value, file_source=fi.file_source,
        file_conflicts=tuple(row_fact.get("conflicts") or ()),
        user_value=fi.user_value, user_entry=fi.user_entry, user_reason=fi.user_reason,
        used_value=fi.used_value, used_source=fi.used_source, disagreement=fi.disagreement, fallback=fallback)


def fact_rows(rows: Mapping[str, dict], index: inputs.EntryIndex, calibration: Mapping[str, dict], check,
              folder_now: str | None, scans: Iterable[str] | None = None) -> list[FactRow]:
    """Per scan: temperature, slit, then Q and counts per analyzer. ``rows`` is the import record,
    ``calibration`` the card state's tabulated calibration, ``check`` the persisted curve check (or None)."""
    out: list[FactRow] = []
    for scan in sorted(rows if scans is None else set(scans) & set(rows), key=lambda k: _order(rows, k)):
        row = rows[scan]
        si = inputs.scan_inputs(row, index)
        row_facts = row.get("facts") or {}
        t, s = si.facts["temperature"], si.facts["slit"]
        out.append(_scan_fact_row(scan, "temperature", t, row_facts.get(inputs.FILE_FACT["temperature"]) or {},
                                  "bose_t0_limit" if t.used_value is None else None))
        out.append(_scan_fact_row(scan, "slit", s, row_facts.get(inputs.FILE_FACT["slit"]) or {},
                                  _slit_fallback(row, si, calibration.get(scan), check, folder_now)
                                  if s.used_value is None else None))
        for det, a in sorted((row.get("analyzers") or {}).items()):
            q = a.get("q")
            out.append(FactRow(
                scan=scan, analyzer=det, fact="q", unit=None, enterable=False, file_value=q,
                file_source="missing" if q is None else a.get("q_source") or "missing", file_conflicts=(),
                user_value=None, user_entry=None, user_reason=None, used_value=q,
                used_source="missing" if q is None else "file", disagreement=False, fallback=None))
            counts = a.get("counts")
            if counts is not None:           # accepting counts never changes them: used is always the file value
                acc = si.counts_accepted.get(det)
                out.append(FactRow(
                    scan=scan, analyzer=det, fact="counts", unit=None, enterable=False, file_value=counts,
                    file_source="inferred", file_conflicts=(), user_value=True if acc else None,
                    user_entry=acc.entry if acc else None, user_reason=acc.reason if acc else None,
                    used_value=counts, used_source="file", disagreement=False, fallback=None))
    return out


def _now_args(used: dict, check, folder_now: str | None) -> dict:
    """The ``*_now`` arguments of ``inputs.changes``: the folder saved now, and the library and curve
    identity from the last check while that check was of this folder (else omitted)."""
    args: dict = {"folder_now": folder_now}
    cal = used.get("calibration") if isinstance(used, dict) else None
    if cal is None or check is None or check.folder != folder_now:
        return args
    args["library_now"] = check.library
    file = (cal.get("curve") or {}).get("file")
    if file is not None:
        args["curve_sha_now"] = next((c.sha256 for c in check.curves if c.file == file), "missing")
    return args


def _pass(p, current: dict | None, check, folder_now: str | None) -> PassInputs:
    changed: tuple[Change, ...] = ()
    if current is not None:
        try:
            changed = inputs.changes(p.inputs, current, **_now_args(p.inputs, check, folder_now))
        except (KeyError, TypeError, AttributeError) as exc:      # a line this version cannot compare
            log.warning("fit inputs of %s (%s pass, run %s) cannot be compared with the inputs now (%s)",
                        p.unit, p.pass_name, p.run, exc)
    return PassInputs(pass_name=p.pass_name, state=p.state, run=p.run, started_at=p.started_at,
                      finished_at=p.finished_at, status=p.status, inputs=p.inputs, fallbacks=p.fallbacks,
                      result=p.result, changes=changed)


def fit_inputs(passes: Mapping[str, Mapping[str, object]], rows: Mapping[str, dict], index: inputs.EntryIndex,
               calibration: Mapping[str, dict], check, folder_now: str | None,
               units: Iterable[str] | None = None) -> list[UnitFitInputs]:
    """Per unit with a counting pass (``fit_inputs.latest_passes``): its latest fast and deep pass and the
    changes since, each pass judged on its own. A unit whose scan has no import row is listed without changes."""
    out: list[UnitFitInputs] = []
    scan_inputs: dict[str, inputs.ScanInputs | None] = {}
    wanted = passes if units is None else set(units) & set(passes)
    for unit in sorted(wanted, key=lambda u: (_order(rows, u.rpartition(":")[0]), u)):
        by = passes[unit]
        any_pass = next(iter(by.values()))
        scan, _, det = unit.rpartition(":")
        if scan not in scan_inputs:
            scan_inputs[scan] = inputs.scan_inputs(rows[scan], index) if scan in rows else None
        si = scan_inputs[scan]
        current = None if si is None else inputs.unit_inputs(rows[scan], si, det, calibration.get(scan))
        out.append(UnitFitInputs(
            unit=unit, scan=any_pass.scan or scan, analyzer=det,
            fast=_pass(by["fast"], current, check, folder_now) if "fast" in by else None,
            deep=_pass(by["deep"], current, check, folder_now) if "deep" in by else None))
    return out
