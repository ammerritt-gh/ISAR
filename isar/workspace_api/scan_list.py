"""The Qt-free scan-list read: the value objects behind the GUI's scan list and scan inspector.

One function over the open experiment's read-only facade, :func:`scan_list`, returns a frozen
:class:`ScanListSnapshot`: scan sets, their analyzers (spectra), the files that were not read, and
pooled masters. This module is Qt-free (hard rule 6) and read-only (hard rule 7): it never opens a
file, re-fits, writes or reads ``results.csv``.

The dataclasses, constants and lookup helpers here are final. :func:`scan_list` never raises: the
window calls it on every bind, and an empty workspace gives an empty snapshot.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# Re-exported so the GUI (isar_gui/theme.py) takes the results writer's words from the facade and
# never imports isar.session.* itself (the G16 import contract).
from isar.session.outcome import NOT_FITTED, NOT_RECORDED
from .scan_keys import scan_prefix, split_key

PARSED_OK = "ok"          # Parsed: the file was read; nothing is missing from T and Q
NOT_READ = "not read"     # Parsed: a file that was not read
NOT_RUN = "not run"       # Fast/Deep: no result stored and nothing holds it
HELD = "held"             # Fast: the scan is held (the word results.csv writes for it)

__all__ = [
    "PARSED_OK", "NOT_READ", "NOT_RUN", "HELD", "NOT_FITTED", "NOT_RECORDED",
    "Fact", "Status", "SpectrumRow", "UnreadFile", "ScanSetRow", "PooledRow", "ScanListSnapshot",
    "natural_key", "scan_list",
]


@dataclass(frozen=True)
class Fact:
    value: float | tuple[float, ...] | str | None   # None = missing
    unit: str | None          # "K", "nm⁻¹", "rlu"; None for a unitless fact (the slit text)
    source: str               # the stored source word ("header", "trailer", "filename", "sidecar",
                              # "card", "inferred", "scan", "fit") or "missing"
    origin: str               # "file" | "card" | "inferred" | "fit" | "missing"; stage two adds "user"
    reason: str | None = None # why it is missing (hover text); None when value is present


@dataclass(frozen=True)
class Status:
    word: str                 # shown verbatim, never renamed
    stored: bool              # Parsed: True when the file was read. Fast/Deep: True when the word
                              # comes from that pass's own stored record ("through the pass")
    reason: str | None = None # tooltip text: the stored reason, the hold reason, what is missing


@dataclass(frozen=True)
class SpectrumRow:
    key: str                  # unit_key(experiment, scan_number, detector): THE identity
    set_key: str              # scan_prefix(key): its scan set's identity
    analyzer: str | None      # detector id, e.g. "a06"
    q: Fact
    parsed: Status
    fast: Status
    deep: Status
    file_name: str | None     # the analyzer's own file name; None when it cannot be told
    file_path: str | None
    n_points: int | None
    fit: Any = field(default=None, compare=False, repr=False)  # the FitView the display shows; None: raw counts


@dataclass(frozen=True)
class UnreadFile:
    path: str                 # as the manifest holds it (resolved absolute)
    name: str
    reason: str
    set_key: str | None = None   # set, when it is a member left out of a set that was read


@dataclass(frozen=True)
class ScanSetRow:
    key: str                  # scan_prefix of its spectra: "experiment:scan_number"
    label: str                # the display label, never the hashed placeholder number
    experiment_id: str | None
    scan_numbers: tuple[int, ...]   # as the files list them; () when they list none
    summed: bool              # the parser flagged it pre_combined_input
    n_merged: int | None      # the sidecar's merged-scan count, when the files give it
    temperature: Fact
    slit: Fact
    spectra: tuple[SpectrumRow, ...]      # natural order of analyzer
    unread: tuple[UnreadFile, ...] = ()   # member files of this set that were not read
    expected_analyzers: int | None = None # stage one: always None; stage two: from the backend
    files_received: int = 0   # the parse summary's denominator: distinct manifest files (ws.files entries) of the set


@dataclass(frozen=True)
class PooledRow:
    key: str                  # the master's unit key
    label: str                # "pooled master (N members)"
    temperature: Fact
    q: Fact
    n_members: int | None
    fast: Status
    deep: Status
    fit: Any = field(default=None, compare=False, repr=False)  # the master's FitView


@dataclass(frozen=True)
class ScanListSnapshot:
    sets: tuple[ScanSetRow, ...] = ()
    files_not_read: tuple[UnreadFile, ...] = ()   # every unread file, left-out members included
    pooled: tuple[PooledRow, ...] = ()

    @property
    def n_spectra(self) -> int:
        """Spectra in sets; pooled masters are not counted."""
        return sum(len(s.spectra) for s in self.sets)

    def spectrum(self, key: str) -> SpectrumRow | PooledRow | None:
        """The spectrum or pooled master with this unit key, or ``None``."""
        for s in self.sets:
            for sp in s.spectra:
                if sp.key == key:
                    return sp
        for p in self.pooled:
            if p.key == key:
                return p
        return None

    def scan_set(self, set_key: str) -> ScanSetRow | None:
        """The scan set with this key, or ``None``."""
        for s in self.sets:
            if s.key == set_key:
                return s
        return None


_DIGITS = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """Sort key where digit runs compare as numbers and text compares case-insensitively:
    ``"a3" < "a14"``, ``"58" < "102"``."""
    parts = _DIGITS.split("" if text is None else str(text))
    # re.split puts digit runs at odd positions; a digit run sorts before text at the same position.
    return tuple((0, int(p), "") if i % 2 else (1, 0, p.casefold())
                 for i, p in enumerate(parts) if p != "")


_FILE_SOURCES = ("header", "trailer", "filename", "sidecar")   # facts read from the data file itself
_NOT_IN_FILE = "not in the file"
_LEFT_OUT = "left out of this scan set"


def _missing(reason: str = _NOT_IN_FILE) -> Fact:
    return Fact(None, None, "missing", "missing", reason)


def _origin(source: str) -> str:
    """A fact read from the data file itself is ``file``; any other stored word is its own origin."""
    return "file" if source in _FILE_SOURCES else source


def _fact(stored: dict | None, unit: str | None) -> Fact:
    """A ledger fact (``{"value", "source"}``) as a :class:`Fact`; a fact with no value is missing."""
    stored = stored or {}
    value, source = stored.get("value"), stored.get("source") or "missing"
    if value is None or source == "missing":
        return _missing()
    return Fact(tuple(value) if isinstance(value, list) else value, unit, source, _origin(source))


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _patterns(ws) -> list:
    """The card's measurement filename patterns; none when the workspace has no card policy."""
    policy = getattr(getattr(ws, "card", None), "policy", None)
    return list((getattr(policy, "files", None) or {}).get("measurement") or [])


def _analyzer_number(name: str, patterns) -> int | None:
    """The analyzer number the first card pattern that matches the file name reads from it."""
    for pattern in patterns:
        m = re.fullmatch(pattern, name)
        if m:
            group = m.groupdict().get("analyzer")
            return int(group) if group and group.isdigit() else None
    return None


def _detector_number(detector: str | None) -> int | None:
    m = re.search(r"(\d+)$", detector or "")
    return int(m.group(1)) if m else None


def _q(sv, analyzer_facts: dict) -> Fact:
    """The analyzer's Q: its ``Q_nm`` fact, else the scan's hkl under the stored ``q_source``."""
    q_nm = _fact(analyzer_facts.get("Q_nm"), "nm⁻¹")
    if q_nm.value is not None:
        return q_nm
    source = (sv.provenance or {}).get("q_source")
    if sv.hkl is not None and source not in (None, "none"):
        return Fact(tuple(sv.hkl), "rlu", source, _origin(source))
    return _missing()


def _fast(sv, fv) -> Status:
    """The fast pass as the ledger stored it: no live mark, since the ledger holds none."""
    if fv is None:
        reason = "; ".join(sv.reasons) or sv.status
        if sv.status.startswith("held"):
            return Status(HELD, False, reason)
        if sv.status == "ingested":
            return Status(NOT_RUN, False)
        return Status(NOT_FITTED, False, reason)
    if not fv.fast_pass.get("state"):
        return Status(NOT_RECORDED, False, "no fast-pass record")
    return Status(fv.fast_pass["state"], True)


def _deep(fv) -> Status:
    if fv is not None and fv.deep_pass.get("state"):
        return Status(fv.deep_pass["state"], True, fv.deep_pass.get("reason"))
    return Status(NOT_RUN, False)


def _spectrum(sv, set_key: str, f, t_known: bool) -> SpectrumRow:
    prov = sv.provenance or {}
    det = split_key(sv.scan_id)[2]
    fv = sv.detectors.get(det)
    q = _q(sv, prov.get("analyzer_facts") or {})
    gaps = [g for g, missing in (("T", not t_known), ("Q", q.value is None)) if missing]
    parsed = (Status(PARSED_OK, True) if not gaps else
              Status(", ".join(f"no {g}" for g in gaps), True,
                     "read; " + "; ".join(f"{g} is not in the file" for g in gaps)))
    return SpectrumRow(
        key=sv.scan_id, set_key=set_key, analyzer=det, q=q, parsed=parsed,
        fast=_fast(sv, fv), deep=_deep(fv),
        file_name=Path(f.path).name if f is not None else None,
        file_path=f.path if f is not None else None,
        n_points=_int_or_none(prov.get("n_points")),
        fit=fv if sv.status == "fitted" and fv is not None else None)


def _scan_set(set_key: str, entries: list, files: list, patterns) -> tuple[ScanSetRow, list]:
    """One scan set from its ledger entries (analyzers, natural order) and its manifest files;
    returns the row and the member files it leaves out."""
    first = entries[0]
    prov = first.provenance or {}
    facts = prov.get("scan_facts") or {}
    listed = (facts.get("scan_numbers") or {}).get("value")
    scan_numbers = tuple(listed) if isinstance(listed, list) and all(isinstance(n, int) for n in listed) else ()
    if "T_K" in facts:
        temperature = _fact(facts["T_K"], "K")
    elif first.T is not None:
        temperature = Fact(first.T, "K", "scan", "file")
    else:
        temperature = _missing()
    label = (str(scan_numbers[0]) if len(scan_numbers) == 1
             else prov.get("scan_label") or str(first.scan_number))

    by_number = {}
    for f in files:
        number = _analyzer_number(Path(f.path).name, patterns)
        if number is not None:
            by_number[number] = f
    spectra, used = [], set()
    for sv in entries:
        f = files[0] if len(files) == 1 else by_number.get(_detector_number(split_key(sv.scan_id)[2]))
        if f is not None:
            used.add(f.path)
        spectra.append(_spectrum(sv, set_key, f, temperature.value is not None))

    flag = next((fl.get("reason") for fl in prov.get("scan_flags") or []
                 if fl.get("type") == "missing_analyzer"), None) or _LEFT_OUT
    left_out = [UnreadFile(path=f.path, name=Path(f.path).name, reason=flag, set_key=set_key)
                for f in files if f.path not in used]
    row = ScanSetRow(
        key=set_key, label=label, experiment_id=split_key(first.scan_id)[0],
        scan_numbers=scan_numbers,
        summed=any(fl.get("type") == "pre_combined_input" for fl in prov.get("scan_flags") or []),
        n_merged=_int_or_none((facts.get("n_merged_scans") or {}).get("value")),
        temperature=temperature, slit=_fact(facts.get("slit"), None), spectra=tuple(spectra),
        unread=tuple(sorted(left_out, key=lambda u: natural_key(u.name))),
        files_received=len(files))
    return row, left_out


def _pooled(sv, fv) -> PooledRow:
    n = fv.n_members
    q = fv.q
    return PooledRow(
        key=sv.scan_id,
        label=f"pooled master ({n} members)" if n is not None else "pooled master",
        temperature=Fact(fv.T, "K", "fit", "fit") if fv.T is not None else _missing("not in the fit"),
        q=(Fact(tuple(q), "rlu", "fit", "fit") if q and all(v is not None for v in q)
           else _missing("not in the fit")),
        n_members=n, fast=_fast(sv, fv), deep=_deep(fv), fit=fv)


def _unread_reason(f) -> str | None:
    """Why a manifest file was not read, or None when it was read."""
    if f.error:
        return f.error
    if f.classification == "unrecognized":
        return "not recognised as a data file"
    if f.classification == "foreign":
        return "a data file of another instrument"
    if f.classification == "measurement" and not f.scan_ids and not f.n_context:
        return "no scan was read from it"
    return None


def scan_list(ws) -> ScanListSnapshot:
    """The scan list of the open experiment ``ws`` (a ``Workspace`` or anything with its read
    surface, such as the GUI's null workspace). Read-only; never raises on an empty workspace."""
    patterns = _patterns(ws)
    files = list(ws.files)
    members: dict[str, list] = {}   # scan-set key -> the manifest files that hold its scans
    for f in files:
        if f.classification == "measurement":
            for sid in f.scan_ids:
                members.setdefault(sid, []).append(f)

    by_set: dict[str, list] = {}
    pooled = []
    for sv in ws.scans:
        fv = sv.detectors.get(split_key(sv.scan_id)[2])
        if fv is not None and fv.pooled:
            pooled.append(_pooled(sv, fv))
        else:
            by_set.setdefault(scan_prefix(sv.scan_id), []).append(sv)

    unread: dict[str, UnreadFile] = {}   # manifest path -> every file not read
    sets = []
    for set_key, entries in by_set.items():
        entries.sort(key=lambda sv: natural_key(split_key(sv.scan_id)[2]))
        row, left_out = _scan_set(set_key, entries, members.get(set_key, []), patterns)
        sets.append(row)
        for u in left_out:
            unread.setdefault(u.path, u)
    for f in files:
        reason = _unread_reason(f)
        if reason is not None and f.path not in unread:
            unread[f.path] = UnreadFile(path=f.path, name=Path(f.path).name, reason=reason)

    counts = Counter(s.label for s in sets)
    sets = [replace(s, label=f"{s.label} [{s.experiment_id}]") if counts[s.label] > 1 else s
            for s in sets]
    return ScanListSnapshot(
        sets=tuple(sorted(sets, key=lambda s: (natural_key(s.label), s.key))),
        files_not_read=tuple(sorted(unread.values(), key=lambda u: natural_key(u.name))),
        pooled=tuple(sorted(pooled, key=lambda p: p.key)))
