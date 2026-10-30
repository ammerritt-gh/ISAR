"""Spectrum keys, series keys and temperatures for the inter-spectra graph (C-keys). Stdlib at module level; no Qt.

Read-only: ``read_nodes`` reads the import record, the intake entries and the ledger, and never writes.
The numerics and the workspace are imported inside the functions, so the module loads without them.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class SpectrumNode:
    key: str            # "<experiment>:<scan_number>:<detector>"  (FileItem.unit_key, ScanEntry.scan_id)
    scan: str           # "<experiment>:<scan_number>"             (row["scan"], scan_prefix(key))
    detector: str       # "a03"
    label: str          # the set stem for messages, e.g. "Smp_100K_0p5_0p5_3" (row["set"])
    series: str | None  # series key; None: no q setting recorded, so no T-links
    T: float | None     # K in force (user value over file value); None: none
    eligible: bool      # has a usable fast solo fit
    reason: str | None  # why not eligible / why no series / why no T; None when all fine


@dataclass(frozen=True)
class NodeSet:
    nodes: dict[str, SpectrumNode]          # key -> node, insertion order = natural scan then detector order
    series: dict[str, tuple[str, ...]]      # series key -> scan keys ordered by T slot (ties by scan number)
    slots: dict[str, float]                 # scan key -> T slot value (t_key binned), only scans with T
    repeats: tuple[str, ...]                # one line per series slot holding >1 scan (assumption U-b)


def _natural(text: str) -> tuple:
    """Sort key that puts 'a9' before 'a10' and 'SPEC_2' before 'SPEC_10'."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", text) if p)


def series_key(row: dict, *, sample: str | None, hkl_step: float) -> str | None:
    from isar.reduce.collate import _q_key      # collate imports numpy; kept out of module import

    hkl = ((row.get("facts") or {}).get("hkl_nominal") or {}).get("value")
    if isinstance(hkl, list) and len(hkl) == 3:
        i, j, k = _q_key(hkl, hkl_step)
        return f"{sample or ''}|q:{i},{j},{k}@{hkl_step}"
    qs = {det: a.get("q") for det, a in (row.get("analyzers") or {}).items()}
    if qs and all(q is not None for q in qs.values()):
        keyed = {det: list(_q_key(q, hkl_step)) for det, q in sorted(qs.items())}
        digest = hashlib.sha256(json.dumps(keyed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return "aq:" + digest[:16]
    return None


def _ineligible(si, det: str, entry) -> str | None:
    if si.excluded is not None:
        return "scan excluded"
    if det in si.dead:
        return "declared dead"
    if entry is None:
        return "not fitted yet"
    if entry.status == "fitted" and det in entry.detectors:
        return None
    if entry.reasons:
        return str(entry.reasons[0])
    return entry.status if entry.status != "fitted" else "not fitted yet"


def read_nodes(root) -> NodeSet:
    from isar.intake import inputs, scans as intake_scans, store
    from isar.reduce.collate import _t_key
    from isar.session.workspace import Workspace

    card, session = Workspace.open(root).load_read_only()
    pol = card.policy
    sample = (pol.sample or {}).get("name") or ""
    t_round = (pol.collation or {}).get("t_round", 0.1)
    rows = intake_scans.read_scans(root)
    read = store.read_all(root, store.ENTRIES)
    index = inputs.index_entries(read.entries, lines=read.lines)

    nodes: dict[str, SpectrumNode] = {}
    members: dict[str, list[tuple[float, int, str]]] = {}     # series key -> (slot, scan number, scan key)
    slots: dict[str, float] = {}
    labels: dict[str, str] = {}
    for scan, row in sorted(rows.items(), key=lambda kv: _natural(kv[0])):
        si = inputs.scan_inputs(row, index)
        used = si.facts["temperature"].used_value
        T = None if used is None else float(used)
        series = series_key(row, sample=sample, hkl_step=pol.hkl_round_step)
        labels[scan] = row["set"]
        if T is not None:
            slots[scan] = _t_key(T, t_round)
            if series is not None:
                members.setdefault(series, []).append((slots[scan], int(row["scan_number"]), scan))
        scan_reason = None
        if series is None:
            scan_reason = "no q setting recorded for this scan"
        elif T is None:
            scan_reason = "no temperature"
        for det in sorted(row.get("analyzers") or {}, key=_natural):
            key = f"{scan}:{det}"
            why = _ineligible(si, det, session.scans.get(key))
            nodes[key] = SpectrumNode(key, scan, det, row["set"], series, T, why is None, why or scan_reason)

    series_out: dict[str, tuple[str, ...]] = {}
    repeat_lines: list[str] = []
    for series, found in members.items():
        ordered = sorted(found, key=lambda m: (m[0], m[1]))
        series_out[series] = tuple(scan for _, _, scan in ordered)
        for slot, n in sorted(Counter(m[0] for m in ordered).items()):
            if n > 1:
                names = ", ".join(labels[scan] for s, _, scan in ordered if s == slot)
                repeat_lines.append(f"{series}: {slot:g} K holds {n} scans ({names}); no temperature link touches them")
    return NodeSet(nodes=nodes, series=series_out, slots=slots, repeats=tuple(repeat_lines))
