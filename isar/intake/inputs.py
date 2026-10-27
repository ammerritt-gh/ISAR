"""Effective inputs: each scan's file values beside the user values in force, and what a fit used.

Read from the import record (``report/intake_scans.json``, file values) and ``intake/entries.jsonl`` (user
values and conditions). ``apply`` builds the effective record as a copy; the parsed record, its file
facts and its counts are never changed. ``unit_inputs`` is the per-analyzer record a fit is tied to
(``report/fit_inputs.jsonl``), ``inputs_hash`` its identity, and ``changes`` what differs between the
inputs a pass used and those in force now. Imports only stdlib, numpy and ``isar.schema`` at module level
(no Qt, no scipy, no ``isar.session``).

In force, for a (target, fact): the highest-``seq`` ``value`` or ``file_value`` entry naming the target
(``file_value`` means no user value). For a (target, condition): the highest-``seq`` ``condition`` entry
naming it, in force when ``declared``. Nothing is ever removed.
"""
from __future__ import annotations

import copy
import dataclasses
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from numbers import Real
from pathlib import Path

import numpy as np

from isar.intake import settings, store

log = logging.getLogger(__name__)

ENTERABLE = {"temperature": "K", "slit": "mm"}      # fact -> its one unit
FILE_FACT = {"temperature": "T_K", "slit": "slit"}  # fact -> the scan fact name in the parsed record
CONDITIONS = ("excluded", "dead", "counts_accepted")   # excluded: scan targets; dead, counts_accepted: unit targets
FALLBACKS = ("bose_t0_limit", "slit_inferred", "slit_broadest_curve", "zero_file_axis", "counts_accepted")
_LIBRARY_BASES = ("inferred", "broadest_curve_fallback")   # a slit chosen by comparing curves
_SLIT_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def read_entries(root) -> list[dict]:
    return store.read(root, store.ENTRIES)


@dataclass(frozen=True)
class EntryIndex:
    facts: Mapping[tuple[str, str], dict]               # (scan key, fact) -> in-force value/file_value entry
    conditions: Mapping[tuple[str, str], dict]          # (target, condition) -> latest condition entry
    units: Mapping[tuple[str, str], Mapping[str, dict]]  # (scan key, condition) -> analyzer -> latest entry
    damaged: tuple[store.Damaged, ...] = ()             # entries that break the entry format, skipped


def _entry_problem(e: dict) -> str | None:
    kind = e.get("kind")
    if kind in ("value", "file_value"):
        fact = e.get("fact")
        if fact not in ENTERABLE:
            return f"unknown fact {fact!r}"
        if kind == "value":
            v = e.get("value")
            ok = (isinstance(v, Real) and not isinstance(v, bool) and math.isfinite(v) and v > 0) \
                if fact == "temperature" else isinstance(v, str)
            if not ok:
                return f"{v!r} is not a {fact} value"
    elif kind == "condition":
        if e.get("condition") not in CONDITIONS:
            return f"unknown condition {e.get('condition')!r}"
        if not isinstance(e.get("declared"), bool):
            return "'declared' is not true or false"
    else:
        return f"unknown kind {kind!r}"
    t = e.get("targets")
    if not isinstance(t, list) or not all(isinstance(x, str) for x in t):
        return "targets is not a list of keys"
    if not (e.get("reason") is None or isinstance(e.get("reason"), str)):
        return "reason is not text"
    return None


def index_entries(entries: list[dict], *, lines: Sequence[int] | None = None) -> EntryIndex:
    """Index the entries once per read. An entry breaking the entry format is skipped, logged and listed
    in ``damaged``; its line is ``lines[i]`` (``store.read_all(...).lines``), else its ``seq``."""
    facts: dict = {}
    conditions: dict = {}
    units: dict = {}
    damaged = []
    for i, e in enumerate(entries):
        why = _entry_problem(e)
        if why is not None:
            line = lines[i] if lines is not None else e["seq"]
            log.warning("%s line %d skipped: %s", store.ENTRIES, line, why)
            damaged.append(store.Damaged(store.ENTRIES, line, why))
            continue
        if e["kind"] == "condition":
            cond = e["condition"]
            for t in e["targets"]:
                prev = conditions.get((t, cond))
                if prev is None or e["seq"] > prev["seq"]:
                    conditions[(t, cond)] = e
                    if cond != "excluded":
                        scan, _, det = t.rpartition(":")
                        units.setdefault((scan, cond), {})[det] = e
        else:
            for t in e["targets"]:
                prev = facts.get((t, e["fact"]))
                if prev is None or e["seq"] > prev["seq"]:
                    facts[(t, e["fact"])] = e
    return EntryIndex(facts, conditions, units, tuple(damaged))


def damage_lines(root) -> list[str]:
    """One line per damaged line of both intake files (store-, settings- and entry-level); [] when none."""
    found = list(settings.current_settings(root).damaged)
    r = store.read_all(root, store.ENTRIES)
    found += [*r.damaged, *index_entries(r.entries, lines=r.lines).damaged]
    found.sort(key=lambda d: (d.file != store.SETTINGS, d.line))
    return [f"{d.file} line {d.line} skipped: {d.why}" for d in found]


@dataclass(frozen=True)
class FactInput:
    fact: str                    # "temperature" | "slit"
    file_value: object | None    # float K | "HxV" | None; from the import row's facts
    file_source: str             # the row fact's "source": header|trailer|sidecar|filename|card|inferred|missing
    user_value: object | None    # canonical, from the entry in force; None when none
    user_entry: str | None       # id of the in-force "value" entry
    user_reason: str | None
    used_value: object | None    # user_value if not None else file_value (None when both absent)
    used_source: str             # "user" | "file" | "missing"
    disagreement: bool           # user and file both present and not equal


@dataclass(frozen=True)
class Condition:
    entry: str                   # id of the in-force declaring entry
    reason: str | None           # the user's reason, or None
    at: str                      # that entry's UTC time


@dataclass(frozen=True)
class ScanInputs:
    scan: str
    facts: Mapping[str, FactInput]                       # keys "temperature", "slit"
    excluded: Condition | None                           # the in-force exclusion, else None
    dead: Mapping[str, Condition] = field(default_factory=dict)             # analyzer -> declaration
    counts_accepted: Mapping[str, Condition] = field(default_factory=dict)  # analyzer -> declaration


def _file_value(fact: str, v):
    if fact == "temperature":
        return float(v) if isinstance(v, Real) and not isinstance(v, bool) and math.isfinite(v) else None
    return v if isinstance(v, str) else None


def _equal(fact: str, a, b) -> bool:
    if fact == "temperature":
        return float(a) == float(b)
    return [float(x) for x in _SLIT_NUMBER.findall(a)] == [float(x) for x in _SLIT_NUMBER.findall(b)]


def _condition(e: dict | None) -> Condition | None:
    return Condition(e["id"], e.get("reason"), e["at"]) if e is not None and e["declared"] else None


def scan_inputs(row: dict, index: EntryIndex) -> ScanInputs:
    """One scan's file and user values, the used value, any disagreement, and the conditions in force."""
    scan = row["scan"]
    row_facts = row.get("facts") or {}
    facts = {}
    for fact, name in FILE_FACT.items():
        f = row_facts.get(name) or {}
        file_value = _file_value(fact, f.get("value"))
        e = index.facts.get((scan, fact))
        user = e if e is not None and e["kind"] == "value" else None
        user_value = None if user is None else (float(user["value"]) if fact == "temperature" else user["value"])
        used = user_value if user_value is not None else file_value
        facts[fact] = FactInput(
            fact=fact, file_value=file_value, file_source=f.get("source") or "missing",
            user_value=user_value, user_entry=None if user is None else user["id"],
            user_reason=None if user is None else user.get("reason"),
            used_value=used,
            used_source="user" if user_value is not None else ("file" if file_value is not None else "missing"),
            disagreement=user_value is not None and file_value is not None and not _equal(fact, user_value, file_value))

    def held(cond):
        return {det: c for det, e in sorted(index.units.get((scan, cond), {}).items())
                if (c := _condition(e)) is not None}
    return ScanInputs(scan=scan, facts=facts, excluded=_condition(index.conditions.get((scan, "excluded"))),
                      dead=held("dead"), counts_accepted=held("counts_accepted"))


def apply(record, si: ScanInputs):
    """The effective record: a copy with the user values, without the dead analyzers, and naming the
    analyzers whose counts are accepted. ``record`` itself when nothing changes; it is never mutated."""
    facts = dict(record.facts or {})
    changed = False
    for fact, fi in si.facts.items():
        if fi.user_value is not None:
            name = FILE_FACT[fact]
            own = (record.facts or {}).get(name)
            facts[name] = {"value": fi.user_value, "source": "user", "entry": fi.user_entry,
                           "file": copy.deepcopy(own) if own is not None else None}
            changed = True
    spectra = {det: sp for det, sp in record.spectra.items() if det not in si.dead}
    changed = changed or len(spectra) != len(record.spectra)
    accepted = sorted(det for det in si.counts_accepted if det in spectra)
    if accepted:
        facts["counts_accepted"] = {"value": accepted, "source": "user",
                                    "entries": {det: si.counts_accepted[det].entry for det in accepted}}
        changed = True
    if not changed:
        return record
    t = si.facts["temperature"]
    temperature = record.temperature
    if t.user_value is not None:
        temperature = dataclasses.replace(
            record.temperature, setpoint=np.full(len(record.temperature.setpoint), float(t.user_value)),
            sensors=dict(record.temperature.sensors), valid_mask=np.array(record.temperature.valid_mask, copy=True))
    return dataclasses.replace(record, facts=facts, temperature=temperature, spectra=spectra)


def _canonical_hash(obj) -> str:
    from isar.session.state import canonical_hash   # imported here: the read side never loads isar.session
    return canonical_hash(obj)


def calibration_inputs_key(*, data_revision: str, folder: str | None, pattern: str | None,
                           library: str | None, declared_slit: str | None,
                           analyzers: Sequence[str], counts_accepted: Sequence[str]) -> str:
    """The identity of what a scan's tabulated calibration is computed from (values, never entry ids)."""
    return _canonical_hash({"data_revision": data_revision, "folder": folder, "pattern": pattern,
                            "library": library, "declared_slit": declared_slit,
                            "analyzers": sorted(analyzers), "counts_accepted": sorted(counts_accepted)})


def _finite(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


def _declared(c: Condition | None) -> dict:
    return {"value": c is not None, "entry": None if c is None else c.entry, "reason": None if c is None else c.reason}


def _calibration(state: dict | None, det: str) -> dict | None:
    if state is None:
        return None
    d = (state.get("detectors") or {}).get(det)
    if state.get("error"):
        held = f"tabulated calibration failed for this scan ({state['error']})"
    elif d is None:
        held = f"{det}: no tabulated calibration for this analyzer"
    else:
        held = d.get("held_reason")
    d = d or {}
    return {"folder": state.get("folder"), "pattern": state.get("pattern"), "library": state.get("library"),
            "setting": state.get("setting"), "basis": state.get("setting_basis"),
            "zero": d.get("zero"), "zero_source": d.get("zero_source"), "curve": d.get("curve"),
            "held_reason": held, "key": state.get("inputs_key")}


def unit_inputs(row: dict, si: ScanInputs, det: str, calibration: dict | None) -> dict:
    """One analyzer's inputs as a fit is tied to them (the ``inputs`` of a fit-inputs line).
    ``calibration`` is the scan's tabulated calibration state entry, or None."""
    def fact(f):
        fi = si.facts[f]
        return {"value": fi.used_value, "source": fi.used_source, "entry": fi.user_entry,
                "file_value": fi.file_value, "file_source": fi.file_source}
    a = (row.get("analyzers") or {}).get(det) or {}
    counts = a.get("counts")
    if counts is not None:
        acc = si.counts_accepted.get(det)
        counts = {**counts, "accepted": _declared(acc),
                  "override_applied": counts.get("level") == "warning" and counts.get("family") == "_i"
                  and acc is not None}
    return _finite({
        "data": row.get("data_revision"),
        "temperature": fact("temperature"),
        "slit": fact("slit"),
        "q": {"value": a.get("q"), "source": a.get("q_source", "none")},
        "excluded": _declared(si.excluded),
        "dead": _declared(si.dead.get(det)),
        "dead_in_scan": sorted(d for d in si.dead if d != det),
        "counts": counts,
        "calibration": _calibration(calibration, det),
    })


def _override(unit: dict) -> bool:
    return bool((unit.get("counts") or {}).get("override_applied"))


def inputs_hash(unit: dict) -> str:
    """The hash of exactly the values ``changes`` compares, plus the calibration's setting, basis, zero
    and held reason; reasons, sources, entry ids, ``calibration.key`` and a declared slit's library are
    recorded, never hashed."""
    cal = unit.get("calibration")
    c = None
    if cal is not None:
        c = {k: cal.get(k) for k in ("folder", "setting", "basis", "zero", "zero_source", "held_reason", "curve")}
        if cal.get("basis") in _LIBRARY_BASES:
            c["library"] = cal.get("library")
    return _canonical_hash({
        "data": unit.get("data"), "temperature": unit["temperature"]["value"], "slit": unit["slit"]["value"],
        "q": unit["q"]["value"], "excluded": unit["excluded"]["value"], "dead": unit["dead"]["value"],
        "dead_in_scan": unit["dead_in_scan"], "counts_override": _override(unit), "calibration": c})


def fallbacks(unit: dict) -> tuple[str, ...]:
    """The fallbacks a fit with these inputs runs under, in ``FALLBACKS`` order."""
    cal = unit.get("calibration") or {}
    on = {"bose_t0_limit": unit["temperature"]["value"] is None,
          "slit_inferred": cal.get("basis") == "inferred",
          "slit_broadest_curve": cal.get("basis") == "broadest_curve_fallback",
          "zero_file_axis": cal.get("zero_source") == "pipeline_zero",
          "counts_accepted": _override(unit)}
    return tuple(f for f in FALLBACKS if on[f])


@dataclass(frozen=True)
class Change:
    field: str               # "data"|"temperature"|"slit"|"q"|"excluded"|"dead"|"dead_in_scan"|"counts_accepted"|"resolution_folder"|"curve"|"resolution_library"
    used: object             # the value the pass used
    now: object              # the value in force now
    now_source: str | None   # for temperature/slit: "user"|"file"|"missing"; else None


def changes(used: dict, current: dict, *, folder_now: str | None = None,
            library_now: str | None = None, curve_sha_now: str | None = None) -> tuple[Change, ...]:
    """What differs, by value, between the inputs a pass used and those in force now. A calibration check
    is left out when the pass had no calibration or its ``*_now`` is None; pass ``curve_sha_now="missing"``
    for a curve file that is gone."""
    out = []

    def differ(name, u, n, src=None):
        if u != n:
            out.append(Change(name, u, n, src))
    differ("data", used.get("data"), current.get("data"))
    for f in ("temperature", "slit"):
        differ(f, used[f]["value"], current[f]["value"], current[f]["source"])
    differ("q", used["q"]["value"], current["q"]["value"])
    differ("excluded", used["excluded"]["value"], current["excluded"]["value"])
    differ("dead", used["dead"]["value"], current["dead"]["value"])
    differ("dead_in_scan", used["dead_in_scan"], current["dead_in_scan"])
    differ("counts_accepted", _override(used), _override(current))
    cal = used.get("calibration")
    if cal is not None:
        if folder_now is not None:
            differ("resolution_folder", cal.get("folder"), folder_now)
        if curve_sha_now is not None:
            differ("curve", (cal.get("curve") or {}).get("sha256"), curve_sha_now)
        if library_now is not None and cal.get("basis") in _LIBRARY_BASES:
            differ("resolution_library", cal.get("library"), library_now)
    return tuple(out)
