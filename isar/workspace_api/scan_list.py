"""The Qt-free scan-list read: the value objects behind the GUI's scan list and scan inspector.

One function over the open experiment's read-only facade, :func:`scan_list`, returns a frozen
:class:`ScanListSnapshot`: scan sets, their analyzers (spectra), the files that were not read, and
pooled masters. This module is Qt-free (hard rule 6) and read-only (hard rule 7): it never re-fits,
writes or reads ``results.csv``; the only files it reads are the intake records, through the facade.

The dataclasses, constants and lookup helpers here are final. :func:`scan_list` never raises: the
window calls it on every bind, and an empty workspace gives an empty snapshot.

Stage two (the BL43 intake backend): a scan set the import record holds takes its temperature and slit
from the backend's fact rows (one source per set; every other set keeps the ledger's), the files not
read and the completeness of a set from the import summary, and each pass is judged current or not from
the fit-inputs read. The caller may hand those reads in, so the GUI thread never parses the fit-inputs
log; left out, they are read from ``ws`` here.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# Re-exported so the GUI (isar_gui/theme.py) takes the results writer's words from the facade and
# never imports isar.session.* itself (the G16 import contract).
from isar.intake import readiness
from isar.intake.readiness import ImportSummary, NeedsAttention
from isar.session.outcome import NOT_FITTED, NOT_RECORDED
from . import intake_view
from .intake_view import FactRow, PassInputs, UnitFitInputs
from .intake_view import import_summary as read_import_summary
from .scan_keys import match_member, scan_prefix, split_key

log = logging.getLogger(__name__)

PARSED_OK = "ok"          # Parsed: the file was read; nothing is missing from T and Q
NOT_READ = "not read"     # Parsed: a file that was not read
NOT_RUN = "not run"       # Fast/Deep: no result stored and nothing holds it
HELD = "held"             # Fast: the scan is held (the word results.csv writes for it)

__all__ = [
    "PARSED_OK", "NOT_READ", "NOT_RUN", "HELD", "NOT_FITTED", "NOT_RECORDED",
    "Fact", "Status", "Mark", "SpectrumRow", "UnreadFile", "ScanSetRow", "PooledRow", "ScanListSnapshot",
    "ATTENTION_NOT_READ", "effective_hkl", "hkl_bin", "hkl_step", "natural_key", "scan_list",
]

# What the GUI passes as ``needs_attention`` while its worker's readiness read is pending (the GUI may not
# import readiness): the rf marks then show no icon.
ATTENTION_NOT_READ = NeedsAttention((), {}, False, "not read yet")


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
    word: str                 # the fitting code's word, returned verbatim (the export keeps it); the GUI's
                              # display table (isar_gui/status_words.py) glosses it, this read does not
    stored: bool              # Parsed: True when the file was read. Fast/Deep: True when the word
                              # comes from that pass's own stored record ("through the pass")
    reason: str | None = None # tooltip text: the stored reason, the hold reason, what is missing
    current: bool | None = None   # stage two: the pass's inputs are still current (None: not judged)
    changed: tuple[tuple[str, str, str], ...] = ()   # stage two: (input, from, to) when not current
    provisional: bool = False # deep: the stored record is untrusted (provisional sampler settings)


_NO_FACT = Fact(None, None, "missing", "missing", "not in the file")    # the default of an appended Fact field


@dataclass(frozen=True)
class Mark:
    """A check or a cross for the gpi and rf columns, judged here and only drawn by the GUI."""
    good: bool | None   # True: check; False: cross; None: not read yet (no icon)
    text: str           # accessible text: "gpi: accepted", "rf: curve missing", "rf: all analyzers good", ...
    detail: str         # tooltip: the file name when good, else the reason in plain words


@dataclass(frozen=True)
class SpectrumRow:
    key: str                  # unit_key(experiment, scan_number, detector): THE identity
    set_key: str              # scan_prefix(key): its scan set's identity
    analyzer: str | None      # detector id, e.g. "a06"
    q: Fact                   # the effective hkl (:func:`effective_hkl`), unit "rlu"; missing when none
    parsed: Status
    fast: Status
    deep: Status
    file_name: str | None     # the analyzer's own file name; None when it cannot be told
    file_path: str | None
    n_points: int | None
    fit: Any = field(default=None, compare=False, repr=False)  # the FitView the display shows; None: raw counts
    q_abs: Fact = _NO_FACT    # the file's Q_nm, unit "nm⁻¹"
    gpi: Mark | None = None   # None: the card reads no gpi files
    rf: Mark | None = None    # None: the card's calibration uses no tabulated curves


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
    gpi: Mark | None = None   # summary of its spectra's marks
    rf: Mark | None = None


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
    gpi: Mark | None = None   # summary of its members' marks
    rf: Mark | None = None


@dataclass(frozen=True)
class ScanListSnapshot:
    sets: tuple[ScanSetRow, ...] = ()
    files_not_read: tuple[UnreadFile, ...] = ()   # every unread file, left-out members included
    pooled: tuple[PooledRow, ...] = ()
    has_gpi: bool = False     # the gpi column exists (the card reads gpi files)
    has_rf: bool = False      # the rf column exists (the calibration uses tabulated curves)

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

# ---- what a pass's changed inputs are called (one table, so a notebook reads the same words) ----
_CHANGE_LABELS = {
    "data": "data revision", "temperature": "temperature", "slit": "slit", "q": "Q",
    "excluded": "scan excluded", "dead": "declared dead", "dead_in_scan": "dead analyzers in the scan",
    "counts_accepted": "counts accepted", "resolution_folder": "resolution folder",
    "curve": "resolution curve", "resolution_library": "resolution library",
    "calibration_setting": "calibration slit", "calibration_basis": "slit basis",
    "calibration_zero": "energy zero", "calibration_zero_source": "energy-zero source",
    "calibration_held_reason": "calibration hold"}
_CHANGE_UNITS = {"temperature": "K", "slit": "mm", "calibration_zero": "meV"}
_HASHED = ("data", "curve", "resolution_library")    # shown by their first 8 characters
_SOURCED = ("temperature", "slit")                   # the inputs that carry where the value came from
_SOURCE_WORDS = {"user": "yours"}


def _change_value(field_name: str, value) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_change_value(field_name, v) for v in value) or "none"
    if field_name in _HASHED:
        return str(value)[:8]
    text = f"{value:g}" if isinstance(value, (int, float)) else str(value)
    unit = _CHANGE_UNITS.get(field_name)
    return f"{text} {unit}" if unit else text


def _change_line(p: PassInputs, change) -> tuple[str, str, str]:
    """``(input, from, to)`` of one change, the value with its source where the input has one."""
    name = change.field
    used_source = now_source = None
    if name in _SOURCED:
        entry = p.inputs.get(name) if isinstance(p.inputs, Mapping) else None
        used_source = entry.get("source") if isinstance(entry, Mapping) else None
        now_source = change.now_source

    def shown(value, source) -> str:
        text = _change_value(name, value)
        return f"{text} ({_SOURCE_WORDS.get(source, source)})" if source else text
    return (_CHANGE_LABELS.get(name, name), shown(change.used, used_source), shown(change.now, now_source))


def _judged(status: Status, p: PassInputs | None) -> Status:
    """``status`` with its currentness. Only a pass the ledger holds is judged, and only a finished one: a
    running pass's inputs are the refit's, not the stored fit's, so the stored fit waits for the re-read."""
    if p is None or p.state != "finished" or not status.stored:
        return status
    return replace(status, current=not p.changed, changed=tuple(_change_line(p, c) for c in p.changes))


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


def _missing_hkl(reason: str) -> Fact:
    return Fact(None, "rlu", "missing", "missing", reason)


def _ledger_hkl(sv) -> Fact:
    """The ledger entry's own hkl under its stored ``q_source`` (``"file"`` when none is stored);
    missing when the entry has none or its source is ``"none"``."""
    source = (sv.provenance or {}).get("q_source")
    if sv.hkl is None or source == "none":
        return _missing_hkl("no hkl in the file")
    return Fact(tuple(float(v) for v in sv.hkl), "rlu", source or "file", "file")


def _unit_hkl(sv, q_rows: Mapping) -> Fact:
    row = q_rows.get((scan_prefix(sv.scan_id), split_key(sv.scan_id)[2]))
    if row is None:
        return _ledger_hkl(sv)
    if row.used_value is None or row.used_source == "missing":
        return _missing_hkl("no hkl in the file, and no accepted gpi file supplies one")
    return Fact(tuple(float(v) for v in row.used_value), "rlu",
                "gpi" if row.used_source == "gpi" else row.file_source, "file")


def effective_hkl(scans, fact_rows: Sequence[FactRow]) -> dict[str, Fact]:
    """Per unit key (``ScanView.scan_id``, ``"exp:scan:aNN"``): the hkl a fit uses, as a :class:`Fact`.

    A unit with a ``q`` fact row (the BL43 import record) takes that row, which ``isar.intake.inputs.analyzer_q``
    already judged: the accepted gpi hkl, else the file's own; a rejected or withdrawn gpi file contributes
    nothing. Any other unit takes its ledger entry's hkl unless its stored ``q_source`` is ``"none"``. Reads
    nothing (both arguments are parsed already), never raises on a malformed entry, returns fresh Facts."""
    q_rows = {(r.scan, r.analyzer): r for r in fact_rows if r.fact == "q" and r.analyzer is not None}
    out: dict[str, Fact] = {}
    for sv in scans:
        try:
            out[sv.scan_id] = _unit_hkl(sv, q_rows)
        except (TypeError, ValueError, AttributeError) as exc:
            log.warning("effective hkl of %s could not be read (%s)", getattr(sv, "scan_id", "?"), exc)
            out[sv.scan_id] = _missing_hkl("the hkl in the record could not be read")
    return out


def hkl_step(ws) -> float:
    """The bin width (rlu) of the auto q-groups, the ``q:`` filter and the pooled hkl: the card's
    ``hkl_round_step`` when it is a positive number, else 0.001."""
    try:
        step = float(getattr(getattr(getattr(ws, "card", None), "policy", None), "hkl_round_step", None))
    except (TypeError, ValueError):
        return 0.001
    return step if step > 0 else 0.001


def hkl_bin(hkl, step: float) -> tuple[int, ...] | None:
    """The integer grid cell of ``hkl`` on ``step``; ``None`` when it is empty, holds a non-number, or
    ``step`` is not positive (such a vector cannot be binned)."""
    if not hkl or step is None or step <= 0:
        return None
    try:
        return tuple(int(round(float(v) / step)) for v in hkl)
    except (TypeError, ValueError):
        return None


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
        dp = fv.deep_pass
        # a refusal states no count, so it has nothing to qualify; no trusted flag means it is not provisional
        return Status(dp["state"], True, dp.get("reason"),
                      provisional=dp["state"] != "refused" and not dp.get("trusted", True))
    return Status(NOT_RUN, False)


def _spectrum(sv, set_key: str, f, t_known: bool, passes: UnitFitInputs | None = None,
              hkl: Mapping[str, Fact] | None = None) -> SpectrumRow:
    prov = sv.provenance or {}
    det = split_key(sv.scan_id)[2]
    fv = sv.detectors.get(det)
    q_abs = _fact((prov.get("analyzer_facts") or {}).get("Q_nm"), "nm⁻¹")
    q = (hkl or {}).get(sv.scan_id) or _ledger_hkl(sv)
    # Parsed keeps its meaning: "no Q" is the file holding neither |Q| nor an hkl, whatever a gpi file adds
    gaps = [g for g, missing in (("T", not t_known),
                                 ("Q", q_abs.value is None and _ledger_hkl(sv).value is None)) if missing]
    parsed = (Status(PARSED_OK, True) if not gaps else
              Status(", ".join(f"no {g}" for g in gaps), True,
                     "read; " + "; ".join(f"{g} is not in the file" for g in gaps)))
    return SpectrumRow(
        key=sv.scan_id, set_key=set_key, analyzer=det, q=q, q_abs=q_abs, parsed=parsed,
        fast=_judged(_fast(sv, fv), passes.fast if passes else None),
        deep=_judged(_deep(fv), passes.deep if passes else None),
        file_name=Path(f.path).name if f is not None else None,
        file_path=f.path if f is not None else None,
        n_points=_int_or_none(prov.get("n_points")),
        fit=fv if sv.status == "fitted" and fv is not None else None)


def _used_fact(row: FactRow | None, unit: str | None) -> Fact:
    """The fact the backend says is in force: the user's value (origin ``user``) or the file's."""
    if row is None or row.used_source == "missing" or row.used_value is None:
        return _missing()
    if row.used_source == "user":
        return Fact(row.used_value, unit, "user", "user")
    return Fact(row.used_value, unit, row.file_source, _origin(row.file_source))


def _scan_set(set_key: str, entries: list, files: list, patterns, intake_facts=None,
              passes=None, hkl=None) -> tuple[ScanSetRow, list]:
    """One scan set from its ledger entries (analyzers, natural order) and its manifest files;
    returns the row and the member files it leaves out. ``intake_facts`` (``{fact: FactRow}`` of the set,
    when the import record holds it) is the one source of its temperature and slit; ``passes`` maps unit
    keys to their fit inputs; ``hkl`` maps unit keys to their effective hkl."""
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
    slit = _fact(facts.get("slit"), None)
    t_in_file = temperature.value is not None     # Parsed judges the file's T, never the used one
    if intake_facts is not None:
        t_row = intake_facts.get("temperature")
        t_in_file = t_row is not None and t_row.file_source != "missing"
        temperature = _used_fact(t_row, "K")
        slit = _used_fact(intake_facts.get("slit"), None)
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
        spectra.append(_spectrum(sv, set_key, f, t_in_file, (passes or {}).get(sv.scan_id), hkl))

    flag = next((fl.get("reason") for fl in prov.get("scan_flags") or []
                 if fl.get("type") == "missing_analyzer"), None) or _LEFT_OUT
    left_out = [UnreadFile(path=f.path, name=Path(f.path).name, reason=flag, set_key=set_key)
                for f in files if f.path not in used]
    row = ScanSetRow(
        key=set_key, label=label, experiment_id=split_key(first.scan_id)[0],
        scan_numbers=scan_numbers,
        summed=any(fl.get("type") == "pre_combined_input" for fl in prov.get("scan_flags") or []),
        n_merged=_int_or_none((facts.get("n_merged_scans") or {}).get("value")),
        temperature=temperature, slit=slit, spectra=tuple(spectra),
        unread=tuple(sorted(left_out, key=lambda u: natural_key(u.name))),
        files_received=len(files))
    return row, left_out


def _pooled(sv, fv, q: Fact) -> PooledRow:
    n = fv.n_members
    return PooledRow(
        key=sv.scan_id,
        label=f"pooled master ({n} members)" if n is not None else "pooled master",
        temperature=Fact(fv.T, "K", "fit", "fit") if fv.T is not None else _missing("not in the fit"),
        q=q, n_members=n, fast=_fast(sv, fv), deep=_deep(fv), fit=fv)


def _members(fv, by_unit: Mapping, by_scan: Mapping) -> list[str] | None:
    """The unit keys of a pooled master's members, by the rule ``Workspace.master_groups`` uses; ``None``
    when it has none or one cannot be resolved."""
    labels = [str(m) for m in fv.members or ()]
    keys = [match_member(label, by_unit, by_scan) for label in labels]
    return None if not keys or any(k is None for k in keys) else list(dict.fromkeys(keys))


def _pooled_hkl(keys: list[str] | None, hkl: Mapping[str, Fact], step: float) -> Fact:
    """The members' component-wise mean when every member has an hkl and all share one bin; never one
    member's value standing for the pool."""
    if keys is None:
        return _missing_hkl("the pool's members are not known")
    facts = [hkl.get(k) for k in keys]
    if any(f is None or f.value is None for f in facts):
        return _missing_hkl("a member has no hkl")
    bins = {hkl_bin(f.value, step) for f in facts}
    if None in bins:
        return _missing_hkl("a member has no hkl")
    if len(bins) > 1:
        return _missing_hkl("the members' hkl differ")
    return Fact(tuple(sum(c) / len(facts) for c in zip(*(f.value for f in facts))), "rlu", "members", "file")


# ---- the gpi and rf marks --------------------------------------------------------------------------
_RF_TEXT = {"missing": "rf: curve missing", "no_slit": "rf: no slit yet", "folder": "rf: folder problem",
            "unchecked": "rf: folder not checked"}


def _reads_gpi(ws) -> bool:
    """Whether the card reads gpi files (BL43LXU)."""
    root = getattr(ws, "root", None)
    if root is None:
        return False
    try:
        return readiness.is_bl43(root)
    except Exception:  # noqa: BLE001 - a read surface degrades: no gpi column, and the reason is logged
        log.exception("scan list: could not tell whether %s reads gpi files", root)
        return False


def _gpi_mark(sp: SpectrumRow, entry, folder_changed: bool, folder: bool) -> Mark:
    """One analyzer's gpi mark: ``entry`` is its scan's gpi entry in force (or None)."""
    if entry is not None:
        name = Path(entry.file).name
        if not entry.accepted:
            return Mark(False, "gpi: not used", entry.problem or f"{name} was not used")
        if sp.q.source == "gpi":
            return Mark(True, "gpi: accepted", name)
        return Mark(False, "gpi: no hkl for this analyzer", f"{name} holds no usable hkl for {sp.analyzer}")
    if folder_changed:
        return Mark(False, "gpi: folder changed", readiness.GPI_FOLDER_CHANGED)
    if not folder:
        return Mark(False, "gpi: no folder", "no gpi folder is chosen")
    return Mark(False, "gpi: no file", "no gpi file matches this scan set")


def _rf_mark(sp: SpectrumRow, attention: NeedsAttention) -> Mark:
    """One analyzer's rf mark, read from the readiness record of its curve."""
    if not attention.evaluated:
        return Mark(None, "rf: not read yet", "ISAR has not read the curve state of this experiment yet")
    sr = attention.scans.get(sp.set_key)
    ar = None if sr is None else sr.analyzers.get(sp.analyzer)
    if ar is None:
        return Mark(None, "rf: not read yet", "ISAR holds no curve state for this analyzer yet")
    if ar.curve is not None:
        return Mark(True, "rf: curve in force", ar.curve)
    text = _RF_TEXT.get(ar.curve_kind, "rf: curve missing")
    return Mark(False, text, ar.curve_reason or text)


def _summary_mark(column: str, marks: Sequence[Mark | None], *, unknown: bool = False) -> Mark | None:
    """The mark of a row over its members' marks: a check only when every one is good."""
    if unknown:
        return Mark(False, f"{column}: members not known", "the pooled master's members cannot be matched to scans")
    if not marks or any(m is None or m.good is None for m in marks):
        return Mark(None, f"{column}: not read yet", "ISAR has not read this yet")
    bad = [m for m in marks if not m.good]
    if not bad:
        return Mark(True, f"{column}: all analyzers good", f"every analyzer's {column} is good")
    return Mark(False, f"{column}: {len(bad)} of {len(marks)} analyzers not good", bad[0].detail)


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


def _read(ws, name: str) -> Sequence:
    """``ws.<name>()``, or nothing for a workspace without that read (the GUI's null workspace)."""
    call = getattr(ws, name, None)
    return call() if callable(call) else ()


def scan_list(ws, *, fact_rows: Sequence[FactRow] | None = None,
              fit_inputs: Sequence[UnitFitInputs] | None = None,
              import_summary: ImportSummary | None = None,
              needs_attention: NeedsAttention | None = None) -> ScanListSnapshot:
    """The scan list of the open experiment ``ws`` (a ``Workspace`` or anything with its read
    surface, such as the GUI's null workspace). Read-only; never raises on an empty workspace.

    The four keywords are the intake backend's reads (``ws.fact_rows()``, ``ws.fit_inputs()``,
    ``import_summary(ws.root)`` and ``needs_attention(ws.root)``); ``None`` reads them here. The GUI passes
    the ones its holder already has, so its thread never parses the fit-inputs log or recomputes the summary
    (and ``ATTENTION_NOT_READ`` while the readiness read is pending)."""
    if fact_rows is None:
        fact_rows = _read(ws, "fact_rows")
    if fit_inputs is None:
        fit_inputs = _read(ws, "fit_inputs")
    if import_summary is None:
        root = getattr(ws, "root", None)
        import_summary = None if root is None else read_import_summary(root)
    has_gpi = _reads_gpi(ws)
    has_rf = import_summary is not None and import_summary.curves is not None
    if needs_attention is None:
        root = getattr(ws, "root", None)
        needs_attention = (intake_view.needs_attention(root) if root is not None and (has_gpi or has_rf)
                           else ATTENTION_NOT_READ)
    hkl = effective_hkl(ws.scans, fact_rows)
    step = hkl_step(ws)
    gpi_entries = (_read(ws, "_gpi_entries") or {}) if has_gpi else {}
    gpi_check = None if import_summary is None else import_summary.gpi
    gpi_folder = gpi_check is not None and gpi_check.folder is not None
    folder_changed = needs_attention.evaluated and any(g.kind == "gpi_folder_changed" for g in needs_attention.groups)
    unit_marks: dict[str, tuple[Mark | None, Mark | None]] = {}
    facts: dict[str, dict[str, FactRow]] = {}    # scan set -> fact -> row, the sets the import record holds
    for row in fact_rows:
        if row.analyzer is None:
            facts.setdefault(row.scan, {})[row.fact] = row
    passes = {u.unit: u for u in fit_inputs}
    summary = import_summary if import_summary is not None and (
        import_summary.n_scans > 0 or import_summary.files_not_read) else None
    missing = {} if summary is None else {i.scan: len(i.missing_analyzers) for i in summary.incomplete_sets}

    patterns = _patterns(ws)
    files = list(ws.files)
    members: dict[str, list] = {}   # scan-set key -> the manifest files that hold its scans
    for f in files:
        if f.classification == "measurement":
            for sid in f.scan_ids:
                members.setdefault(sid, []).append(f)

    by_set: dict[str, list] = {}
    pooled_views = []
    by_unit: dict[str, str] = {}
    by_scan: dict[int, list[str]] = {}
    for sv in ws.scans:
        fv = sv.detectors.get(split_key(sv.scan_id)[2])
        if fv is not None and fv.pooled:
            pooled_views.append((sv, fv))
        else:
            by_set.setdefault(scan_prefix(sv.scan_id), []).append(sv)
            by_unit[sv.scan_id] = sv.scan_id
            if isinstance(sv.scan_number, int):
                by_scan.setdefault(sv.scan_number, []).append(sv.scan_id)

    unread: dict[str, UnreadFile] = {}   # manifest path -> every file not read
    sets = []
    for set_key, entries in by_set.items():
        entries.sort(key=lambda sv: natural_key(split_key(sv.scan_id)[2]))
        row, left_out = _scan_set(set_key, entries, members.get(set_key, []), patterns,
                                  facts.get(set_key), passes, hkl)
        if has_gpi or has_rf:
            spectra = []
            for sp in row.spectra:
                entry = gpi_entries.get(sp.set_key)
                g = _gpi_mark(sp, entry, folder_changed, gpi_folder) if has_gpi else None
                r = _rf_mark(sp, needs_attention) if has_rf else None
                unit_marks[sp.key] = (g, r)
                spectra.append(replace(sp, gpi=g, rf=r))
            row = replace(row, spectra=tuple(spectra),
                          gpi=_summary_mark("gpi", [s.gpi for s in spectra]) if has_gpi else None,
                          rf=_summary_mark("rf", [s.rf for s in spectra]) if has_rf else None)
        if summary is not None and (set_key in missing or set_key in facts):
            row = replace(row, expected_analyzers=len(row.spectra) + missing.get(set_key, 0))
        sets.append(row)
        for u in left_out:
            unread.setdefault(u.path, u)
    for f in files:
        reason = _unread_reason(f)
        if reason is not None and f.path not in unread:
            unread[f.path] = UnreadFile(path=f.path, name=Path(f.path).name, reason=reason)

    if summary is not None:
        unread = {f.path: UnreadFile(path=f.path, name=Path(f.path).name, reason=f.reason, set_key=f.scan)
                  for f in summary.files_not_read}

    pooled = []
    for sv, fv in pooled_views:
        keys = _members(fv, by_unit, by_scan)
        mine = [unit_marks.get(k, (None, None)) for k in keys or ()]
        pooled.append(replace(
            _pooled(sv, fv, _pooled_hkl(keys, hkl, step)),
            gpi=_summary_mark("gpi", [m[0] for m in mine], unknown=keys is None) if has_gpi else None,
            rf=_summary_mark("rf", [m[1] for m in mine], unknown=keys is None) if has_rf else None))

    counts = Counter(s.label for s in sets)
    sets = [replace(s, label=f"{s.label} [{s.experiment_id}]") if counts[s.label] > 1 else s
            for s in sets]
    return ScanListSnapshot(
        sets=tuple(sorted(sets, key=lambda s: (natural_key(s.label), s.key))),
        files_not_read=tuple(sorted(unread.values(), key=lambda u: natural_key(u.name))),
        pooled=tuple(sorted(pooled, key=lambda p: p.key)), has_gpi=has_gpi, has_rf=has_rf)
