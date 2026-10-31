"""Readiness of every BL43 scan, analyzer and use, and the grouped needs-attention list (C-needs-attention).

Computed on read from the persisted records, never stored: the import record (``report/intake_scans.json``),
the entries and settings (``intake/``), the curve check (``report/curves.json``) and the card state's
tabulated calibration. A gap is judged by the stage's own check: the fit stage's ``tabulated.count_hold``,
``curve_need``, ``candidate_gaps`` and ``calibration_reusable``, and the calibration inputs key the scan's
start point stores. Nothing here reads a data file, loads a curve, fits, writes or raises: a record it cannot
read gives ``evaluated=False`` / ``record_problem`` and a log line. Imports no Qt, no scipy and nothing from
``isar.session``; ``isar.parsers.metadata`` only inside the slit helper (it registers every parser plugin).
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from isar.calibrate import tabulated
from isar.intake import curves as curves_mod
from isar.intake import gpi as gpi_mod
from isar.intake import inputs, scans, settings, store
from isar.intake.curves import CurveFile, CurveIndex, OtherFolder, read_curve_check
from isar.schema.card import Card, load_policy, load_state
from isar.schema.scan import Flag

log = logging.getLogger(__name__)

POLICY_FILE = "card.policy.toml"      # isar.session.workspace.POLICY_NAME / STATE_NAME, which this module must not import
STATE_FILE = "card.state.json"

USES = ("read", "reduce", "calibrate", "fit", "steer")             # "reduce" is reduce/export
STATES = ("ready", "fallback", "pending", "held", "unreadable")    # ascending severity
ACTIONS = ("start_analysis", "enter_value", "locate_folder", "reverse_declaration", "accept_counts",
           "import_again", "tell_operator", "wait", "none")

# Fallback labels, shown as given.
T_FALLBACK = "zero-temperature (T → 0⁺) Bose limit"
SLIT_INFERRED = "slit inferred from the elastic lines"
SLIT_BROADEST = "broadest-curve fallback"
ZERO_FALLBACK = "file-axis zero (no significant elastic line)"
NO_MONITOR = "no monitor; exposure recorded as missing"
NO_SIDECAR = "facts from the .var are missing"
LINE_NOT_READ = "line not read"
COUNTS_ACCEPTED = "counts accepted by the user"
LINE_SKIPPED = "line skipped; what it held is not in force"

NOT_BL43 = "readiness is computed for BL43LXU workspaces only"
NOT_CHECKED = "the resolution curves have not been checked yet (at the next import or analysis start)"
FOLDER_CHANGED = "the resolution folder changed; its curves are checked when analysis starts or at the next import"
CURVES_CHANGED = "the resolution curves changed since this scan was calibrated"
GPI_FOLDER_CHANGED = ("the gpi folder changed; the values from the gpi files are set aside until the data is "
                      "read again or analysis starts")
SLIT_PENDING = "no slit in the files or saved; it is chosen from the elastic lines when analysis starts"


@dataclass(frozen=True)
class _Kind:
    title: str
    target: str          # analyzer | scan | experiment | file | line | scan_or_analyzer
    uses: tuple[str, ...]
    state: str           # the group's state when its items carry none of their own
    action: str


_ALL = USES
KINDS: dict[str, _Kind] = {k: _Kind(*v) for k, v in {
    "running": ("Analysis running", "analyzer", (), "running", "wait"),
    "resolution_folder": ("Resolution folder", "experiment", ("calibrate", "fit"), "held", "locate_folder"),
    "curve_missing": ("Resolution curves", "analyzer", ("calibrate", "fit"), "held", "locate_folder"),
    "analyzer_file": ("Analyzer files missing", "analyzer", _ALL, "held", "import_again"),
    "analyzer_unreadable": ("Analyzer files unreadable", "analyzer", _ALL, "unreadable", "import_again"),
    "files_not_read": ("Files not read", "file", ("read",), "unreadable", "import_again"),
    "damaged_lines": ("Intake lines skipped", "line", (), "fallback", "tell_operator"),
    "calibration_failed": ("Calibration failed", "scan", ("calibrate", "fit"), "held", "tell_operator"),
    "not_raw": ("Counts not raw", "analyzer", ("calibrate", "fit"), "held", "tell_operator"),
    "counts_unaccepted": ("Counts may not be raw", "analyzer", ("calibrate", "fit"), "held", "accept_counts"),
    "temperature": ("Temperature", "scan", ("fit",), "fallback", "enter_value"),
    "slit": ("Slit", "scan", ("calibrate", "fit"), "pending", "enter_value"),
    "calibration_pending": ("Calibration pending", "scan", ("calibrate", "fit"), "pending", "start_analysis"),
    "values_changed": ("Values changed since last fit", "analyzer", ("fit",), "ready", "start_analysis"),
    "candidate_coverage": ("Resolution curves at some candidate slits", "analyzer", ("calibrate",),
                           "pending", "locate_folder"),
    "zero_fallback": ("Energy zero from the file axis", "analyzer", ("calibrate", "fit"), "fallback", "none"),
    "sidecar": ("Sidecar (.var)", "scan", ("read",), "fallback", "import_again"),
    "unrecognised_line": ("Unrecognised lines", "analyzer", ("read",), "fallback", "tell_operator"),
    "disagreement": ("File and user values disagree", "scan", ("fit",), "ready", "none"),
    "gpi_rejected": ("gpi file not used", "scan", ("steer", "calibrate", "fit"), "fallback", "import_again"),
    "gpi_curves_dropped": ("gpi curve set set aside", "scan", ("calibrate", "fit"), "ready", "none"),
    "gpi_folder_changed": ("gpi folder changed", "experiment", ("steer", "calibrate", "fit"), "fallback",
                           "import_again"),
    "exposure_missing": ("Monitor missing", "analyzer", ("reduce",), "fallback", "none"),
    "q_missing": ("Q (for steering)", "analyzer", ("steer",), "held", "none"),
    "declared": ("Declared dead or excluded", "scan_or_analyzer", ("reduce", "calibrate", "fit", "steer"),
                 "held", "reverse_declaration"),
}.items()}
KIND_ORDER = tuple(KINDS)
_RANK = {k: i for i, k in enumerate(KIND_ORDER)}
_BLOCKING = ("held", "unreadable")
_SEVERITY = {s: i for i, s in enumerate(STATES)}


@dataclass(frozen=True)
class UseState:
    state: str                    # one of STATES
    reason: str | None            # None when ready
    fallback: str | None          # the fallback label when state == "fallback", else None


@dataclass(frozen=True)
class UnitMarks:                  # from the fit-inputs read (Workspace.fit_inputs)
    running: bool = False
    changed_fast: str | None = None
    changed_deep: str | None = None


@dataclass(frozen=True)
class AnalyzerReadiness:
    unit: str                                 # "exp:scan:aNN"
    analyzer: str
    uses: Mapping[str, UseState]              # every key of USES
    can_start: bool | None                    # None while running
    action: str                               # one of ACTIONS; "none" when nothing is needed
    running: bool
    values_changed: tuple[str, ...]           # fast detail first, then deep; () when unchanged
    curve: str | None = None                  # the resolution curve file ISAR would use for this analyzer now
    curve_kind: str | None = None             # when curve is None: "missing" | "no_slit" | "folder" | "unchecked"
    curve_reason: str | None = None           # when curve is None: the plain reason


@dataclass(frozen=True)
class ScanReadiness:
    scan: str                                 # "exp:scan"
    analyzers: Mapping[str, AnalyzerReadiness]   # read, left out, and declared-dead analyzers, sorted
    uses: Mapping[str, UseState]              # worst over analyzers not declared dead
    can_start: bool | None                    # True if any analyzer can start; None if all running
    action: str


@dataclass(frozen=True)
class AttentionItem:
    scan: str | None                          # None for experiment, file and line items
    analyzer: str | None
    file: str | None                          # files_not_read: the file's path; damaged_lines: the intake file
    reason: str
    fallback: str | None
    running: bool
    values_disagree: bool
    values_changed: tuple[str, ...]


@dataclass(frozen=True)
class AttentionGroup:
    kind: str                                 # a key of KINDS
    title: str
    summary: str
    uses: tuple[str, ...]
    state: str                                # worst over items; "running" for kind running
    action: str
    can_start: bool | None
    fallback_in_use: bool
    n_scans: int
    analyzers: tuple[str, ...]                # distinct analyzers, sorted; () for scan/experiment/file/line groups
    items: tuple[AttentionItem, ...]


@dataclass(frozen=True)
class NeedsAttention:
    groups: tuple[AttentionGroup, ...]        # KIND_ORDER; only non-empty groups
    scans: Mapping[str, ScanReadiness]        # sorted by (exp, int(scan))
    evaluated: bool                           # False: not a BL43LXU workspace, nothing imported, or the record unreadable
    note: str | None                          # why not evaluated


@dataclass(frozen=True)
class IncompleteSet:
    scan: str
    set: str | None                       # file-set stem
    missing_analyzers: Mapping[str, str]  # analyzer -> cause ("no file" or the reader's cause)
    sidecar: str                          # "read" | "missing" | "unreadable" | "not_expected"
    sidecar_reason: str | None


@dataclass(frozen=True)
class FileNotRead:
    path: str
    kind: str                             # files-section class, or "left_out" for a failed member of a read set
    reason: str
    scan: str | None
    analyzer: str | None


@dataclass(frozen=True)
class CurveSummary:
    folder: str | None                    # the SAVED folder (settings), else the card's folder
    source: str                           # "user" | "discovered" | "card" | "none"
    found_beside: str | None
    n_curves: int                         # from the last check; 0 before any check
    analyzers: tuple[str, ...]            # analyzers with at least one curve, sorted
    settings: tuple[str, ...]             # slit settings present, sorted
    problem: str | None
    other_folders: tuple[OtherFolder, ...]
    checked_at: str | None                # None: never checked, or the saved folder is not checked yet
    check_stale: bool                     # the saved folder differs from the one last checked


@dataclass(frozen=True)
class ImportSummary:
    n_scans: int                          # scans in the import record
    n_analyzers: int                      # analyzer spectra read, summed over scans
    incomplete_sets: tuple[IncompleteSet, ...]   # missing analyzers, or sidecar missing/unreadable; sorted by scan
    files_not_read: tuple[FileNotRead, ...]      # sorted by path
    files_not_read_by_kind: Mapping[str, int]
    curves: CurveSummary | None           # None: not a BL43LXU workspace
    last_import_at: str | None            # the latest imported_at over rows
    record_problem: str | None = None     # the import record could not be read (then the counts are zero)
    gpi: gpi_mod.GpiCheck | None = None   # the latest gpi check; None: not BL43, never checked, or unreadable


def scan_order(rows: Mapping[str, dict], scan: str) -> tuple:
    """The sort of scan keys: experiment, scan number, key."""
    row = rows.get(scan) or {}
    n = row.get("scan_number")
    return (str(row.get("experiment_id", scan)), n if isinstance(n, int) else 0, scan)


# ---------------------------------------------------------------------------------------------------------
# reading the records
# ---------------------------------------------------------------------------------------------------------
def _card(root: Path) -> Card:
    return Card(load_policy(root / POLICY_FILE), load_state(root / STATE_FILE))


def is_bl43(root) -> bool:
    """True for a BL43LXU workspace; the one test of whether readiness applies (``evaluate`` makes it too)."""
    root = Path(root)
    return (root / POLICY_FILE).exists() and scans.applies(_card(root))


def _record(root: Path) -> tuple[dict, dict]:
    """``(scan rows, files section)`` of the import record; ValueError when it is malformed."""
    doc = scans._load(root / scans.INTAKE_SCANS)      # {} when nothing was imported
    files = doc.get("files") or {}
    if not isinstance(files, dict):
        raise ValueError(f"{root / scans.INTAKE_SCANS}: the files section is not a mapping")
    return dict(doc.get("scans", {})), dict(files)


def _check(root: Path):
    """``(curve check or None, error text or None)``; a malformed check is read as no check."""
    try:
        return read_curve_check(root), None
    except ValueError as exc:
        log.warning("readiness: the curve check is unreadable (%s); read as not checked", exc)
        return None, str(exc)


def _gpi_check(root: Path):
    """The latest gpi check; a malformed one is logged and read as no check."""
    try:
        return gpi_mod.read_gpi_check(root)
    except ValueError as exc:
        log.warning("readiness: the gpi check is unreadable (%s); read as not checked", exc)
        return None


def _declared(slit) -> str | None:
    """A slit as the calibration declares it (``calibrations._declared_slit``'s rule)."""
    if slit is None:
        return None
    from isar.parsers import metadata    # imported here: it loads isar.parsers, which registers every plugin
    return metadata.normalise_slit(slit) or str(slit)


def _stale(saved, check, error: str | None = None) -> bool:
    """The saved folder differs from the one last checked; never while the check is unreadable (that error is reported)."""
    return error is None and saved is not None and (check is None or check.folder is None or Path(saved.path) != Path(check.folder))


# ---------------------------------------------------------------------------------------------------------
# one scan
# ---------------------------------------------------------------------------------------------------------
@dataclass
class _Gap:
    kind: str
    reason: str
    scan: str | None = None
    analyzer: str | None = None
    file: str | None = None
    fallback: str | None = None
    state: str | None = None             # None: the kind's own
    order: tuple = ()

    def __post_init__(self):
        if self.state is None:
            self.state = KINDS[self.kind].state


def _use(state: str, reason: str | None = None, fallback: str | None = None) -> UseState:
    return UseState(state, reason, fallback)


_READY = _use("ready")


def _counts_view(counts):
    """What ``tabulated.count_hold`` reads of a spectrum, rebuilt from the row's counts record."""
    level = (counts or {}).get("level")
    flags = [Flag("counts_not_integer", "warning" if level == "warning" else "info", "")] if level else []
    return SimpleNamespace(spectrum_flags=flags, facts={"count_recovery": {"value": counts}})


@dataclass
class _Cx:
    """What one scan's calibrate judgement shares across its analyzers."""
    prev: dict | None
    check: object
    cidx: CurveIndex
    slit: str | None             # the declared slit in force; None when none
    current: bool
    failed: bool
    stale: bool
    folder_problem: str | None
    hold_kind: str               # the gap kind of a curve hold: the folder's, or the analyzer's own
    named: str | None = None     # the gpi file's curve set in force; cidx then holds its curves alone


def _pending_reason(cx: _Cx) -> str:
    if cx.stale:
        return FOLDER_CHANGED
    if cx.prev is None:
        return "not calibrated yet; analysis calibrates it"
    if cx.check is not None and cx.prev.get("library") != cx.check.library:
        return CURVES_CHANGED
    return "an input of the calibration changed since it ran (data, slit, dead analyzers or accepted counts)"


def _slit_label(cx: _Cx, flags) -> str | None:
    basis = (cx.prev or {}).get("setting_basis")
    if basis == "inferred":
        ambiguous = any(f.get("type") == "slit_ambiguous" for f in flags)
        return SLIT_INFERRED + (" (ambiguous)" if ambiguous else "")
    return SLIT_BROADEST if basis == "broadest_curve_fallback" else None


def _calibrate(a: str, ar: dict, si: inputs.ScanInputs, cx: _Cx, flags) -> tuple[UseState, str | None]:
    """One analyzer's calibrate use and the kind of gap behind it (``"pending"`` for the scan's pending gap)."""
    why = tabulated.count_hold(_counts_view(ar.get("counts")), accepted=a in si.counts_accepted)
    if why is not None:
        return (_use("held", why), "not_raw") if why == tabulated.NON_RAW_REASON else \
            (_use("held", f"{a}: {why}"), "counts_unaccepted")
    if cx.current:
        d = (cx.prev.get("detectors") or {}).get(a)
        if d is None:
            return _use("held", f"{a}: no tabulated calibration for this analyzer"), cx.hold_kind
        if d.get("held_reason"):
            return _use("held", d["held_reason"]), cx.hold_kind
        zero = d.get("zero_source") == "pipeline_zero"
        label = _slit_label(cx, flags) or (ZERO_FALLBACK if zero else None)
        return (_use("fallback", None, label) if label else _READY), ("zero_fallback" if zero else None)
    if cx.failed:
        return _use("held", f"tabulated calibration failed for this scan ({cx.prev['error']})"), "calibration_failed"
    if cx.stale:
        return _use("pending", FOLDER_CHANGED), "pending"
    if cx.folder_problem is not None:
        return _use("held", f"{a}: {cx.folder_problem}"), "resolution_folder"
    if cx.slit is not None:
        why = tabulated.curve_need(cx.cidx, a, cx.slit)
        if why and cx.named:
            why = f"the curve {cx.named}_{a}, named by the gpi file, is not in the resolution folder"
        return (_use("held", f"{a}: {why}"), "curve_missing") if why else (_use("pending", _pending_reason(cx)), "pending")
    cands = [s for s in cx.cidx.settings() if s]
    if not cands:
        return _use("held", f"{a}: no slit setting is known for the scan"), "curve_missing"
    if len(tabulated.candidate_gaps(cx.cidx, a)) == len(cands):
        return _use("held", f"{a}: no resolution curve for {a} at any slit in the folder"), "curve_missing"
    return _use("pending", _pending_reason(cx)), "pending"


def _curve_in_force(a: str, si: inputs.ScanInputs, cx: _Cx) -> tuple[str | None, str | None, str | None]:
    """``(curve file, kind, reason)``: the resolution curve ISAR would use for ``a`` now, judged on the curve
    alone (not on whether the calibration has run, nor on a counts hold); kind and reason when there is none."""
    if cx.stale:
        return None, "unchecked", FOLDER_CHANGED
    if cx.folder_problem is not None:
        return None, "unchecked" if cx.folder_problem == NOT_CHECKED else "folder", cx.folder_problem
    if si.named is not None:
        file = ((si.gpi.curves or {}).get(a) or {}).get("file")
        if file is not None:
            return file, None, None
        return None, "missing", f"the curve {si.named}_{a}, named by the gpi file, is not in the resolution folder"
    if cx.slit is None:
        # a calibration made from these inputs has chosen a slit: its curve for this analyzer is the one in force
        done = ((cx.prev or {}).get("detectors") or {}).get(a) or {} if cx.current else {}
        if (done.get("curve") or {}).get("file"):
            return done["curve"]["file"], None, None
        return None, "no_slit", SLIT_PENDING
    c = cx.cidx.get(a, cx.slit)
    if c is not None:
        return c.file, None, None
    return None, "missing", tabulated.curve_need(cx.cidx, a, cx.slit)


def _lines_not_read(imp: dict, a: str) -> list[str]:
    out = [f"{x.get('key', '?')}: {x.get('line', '')}".strip() for x in imp.get("unrecognised_lines") or ()
           if x.get("analyzer") == a]
    return out + [str(x.get("line", "")).strip() for x in imp.get("unparsed_lines") or () if x.get("analyzer") == a]


def _declared_reason(what: str, c: inputs.Condition) -> str:
    return f"{what} ({c.reason})" if c.reason else what


def _worst(uses: list[UseState]) -> UseState:
    return max(uses, key=lambda u: _SEVERITY[u.state]) if uses else _READY


def _pick(gaps: list[tuple[str, str]], pending: bool) -> str:
    """What resolves it: the first blocking gap's action, else starting analysis when something is pending,
    else the first remaining gap with an action."""
    ordered = sorted(set(gaps), key=lambda ks: _RANK[ks[0]])
    for k, st in ordered:
        if st in _BLOCKING and KINDS[k].action != "none":
            return KINDS[k].action
    if pending:
        return "start_analysis"
    return next((KINDS[k].action for k, _st in ordered if KINDS[k].action != "none"), "none")


def _can_start(values: list[bool | None]) -> bool | None:
    if any(v is True for v in values):
        return True
    return None if values and all(v is None for v in values) else False


def _scan(scan: str, row: dict, pos: int, index: inputs.EntryIndex, state: dict, check, cidx, folder_problem,
          stale: bool, marks: Mapping[str, UnitMarks], gaps: list[_Gap], an_can: dict, running_scans: set,
          disagree: set, unread: Mapping[str, dict], gpi_entries: Mapping[str, object],
          data_dir: Path) -> ScanReadiness:
    """One scan's readiness; its gaps, the analyzers' ``can_start`` and the scans running or disagreeing are
    added to the collectors the caller passes. ``unread``: path -> its ``files`` entry, for each file the
    latest import recorded failed (the path the row's ``import.paths`` gives for the analyzer's file)."""
    si = inputs.scan_inputs(row, index, gpi_entries.get(scan), data_dir=data_dir)
    cal_slit = _declared(inputs.calibration_slit(si))
    if si.named is not None:       # the named set's curves are the scan's library, at the setting it declares
        cidx = CurveIndex(CurveFile(d, cal_slit, c["file"], c["sha256"]) for d, c in sorted(si.gpi.curves.items()) if c)
    prev = state.get(scan)
    imp = row.get("import") or {}
    read_dets = sorted(row.get("analyzers") or {})
    left_out = imp.get("left_out") or {}
    live = [a for a in read_dets if a not in si.dead]
    key = None if check is None else inputs.calibration_inputs_key(
        data_revision=row["data_revision"], folder=check.folder, pattern=check.pattern, library=check.library,
        declared_slit=cal_slit, analyzers=live, counts_accepted=[a for a in live if a in si.counts_accepted],
        point_masks={d: si.point_masks.get(d, ()) for d in live},
        named=inputs.named_key(si.named, si.gpi.curves if si.gpi else {}))
    current = not stale and key is not None and tabulated.calibration_reusable(
        prev, content_hash=row["data_revision"], library=check.library, inputs_key=key)
    failed = not stale and key is not None and prev is not None and "error" in prev and prev.get("inputs_key") == key
    cx = _Cx(prev, check, cidx, cal_slit, current, failed, stale, folder_problem,
             "resolution_folder" if folder_problem is not None else "curve_missing", si.named)
    flags = row.get("flags") or []
    slit_given = cal_slit is not None        # a user, gpi or file slit, or the named set's own setting
    cands = [s for s in cidx.settings() if s]

    own: dict[str, list[tuple[str, str]]] = {}         # analyzer -> its (kind, state) gaps
    uses_of: dict[str, dict[str, UseState]] = {}
    pending: set[str] = set()
    scan_gaps: list[_Gap] = []

    def gap(kind, reason, a, *, fallback=None, state=None):
        g = _Gap(kind, reason, scan, a, None, fallback, state, (pos, a))
        own.setdefault(a, []).append((kind, g.state))
        gaps.append(g)

    for a in sorted(set(read_dets) | set(left_out) | set(si.dead)):
        if si.excluded is not None or a in si.dead:
            c = si.excluded if si.excluded is not None else si.dead[a]
            why = _declared_reason("scan excluded" if si.excluded is not None else "declared dead", c)
            uses_of[a] = {u: _READY if u == "read" else _use("held", why) for u in USES}
            if si.excluded is None:
                gap("declared", why, a)
            else:
                own.setdefault(a, []).append(("declared", "held"))
            continue
        if (path := (imp.get("paths") or {}).get(a)) in unread:
            # the latest import could not read this analyzer's file: the row is of an earlier read
            rec = unread[path]
            why = f"{a}: {Path(path).name} was not read at the latest import ({rec.get('reason') or rec['class']})"
            uses_of[a] = {u: _use("unreadable" if u == "read" else "held", why) for u in USES}
            gap("analyzer_unreadable", why, a)
            continue
        if a in left_out:
            cause = (left_out[a] or {}).get("cause") or "no file"
            if cause == "no file":
                uses_of[a] = {u: _use("held", f"{a}: no file") for u in USES}
                gap("analyzer_file", f"{a}: no file", a)
            else:
                why = f"{a}: {cause}"
                uses_of[a] = {u: _use("unreadable" if u == "read" else "held", why) for u in USES}
                gap("analyzer_unreadable", why, a)
            continue
        ar = row["analyzers"][a]
        u = {}
        bad = _lines_not_read(imp, a)
        if bad:
            u["read"] = _use("fallback", f"{len(bad)} line(s) not read: {'; '.join(bad[:3])}", LINE_NOT_READ)
            gap("unrecognised_line", u["read"].reason, a, fallback=LINE_NOT_READ)
        else:
            u["read"] = _READY
        if ar.get("exposure") == "missing":
            u["reduce"] = _use("fallback", "no monitor in the file", NO_MONITOR)
            gap("exposure_missing", "no monitor in the file", a, fallback=NO_MONITOR)
        else:
            u["reduce"] = _READY
        cal, kind = _calibrate(a, ar, si, cx, flags)
        u["calibrate"] = cal
        if kind == "pending":
            pending.add(a)
        elif kind is not None:
            if kind == "resolution_folder":
                own.setdefault(a, []).append((kind, "held"))      # the experiment group carries its one item
            elif kind == "calibration_failed":
                own.setdefault(a, []).append((kind, "held"))      # the scan group carries its one item
            elif kind == "zero_fallback":
                gap(kind, "no significant elastic line; the file's own energy axis is used", a,
                    fallback=ZERO_FALLBACK, state="fallback")
            else:
                gap(kind, cal.reason, a, state=cal.state)
        if cal.state in ("held", "pending", "unreadable"):
            fit = cal
        elif si.facts["temperature"].used_value is None:
            fit = _use("fallback", None, T_FALLBACK)
        elif a in si.counts_accepted and inputs.unit_inputs(row, si, a, None)["counts"]["override_applied"]:
            fit = _use("fallback", None, COUNTS_ACCEPTED)
        else:
            fit = cal
        u["fit"] = fit
        if inputs.analyzer_q(row, si, a)[1] == "none":
            u["steer"] = _use("held", "no Q in the files (only |Q|)")
            gap("q_missing", "no Q in the files (only |Q|)", a)
        else:
            u["steer"] = _READY
        # a candidate slit with no curve for this analyzer: only a comparison over the slits is exposed to it
        if (not slit_given and not stale and folder_problem is None and cands
                and kind not in ("not_raw", "counts_unaccepted")
                and (g := tabulated.candidate_gaps(cidx, a)) and len(g) < len(cands)):
            gap("candidate_coverage", f"no curve at {', '.join(g)}; held only if "
                f"{g[0] + ' is' if len(g) == 1 else 'one of them is'} chosen", a)
        uses_of[a] = u

    live_read = [a for a in read_dets if a not in si.dead and si.excluded is None and a not in left_out
                 and (imp.get("paths") or {}).get(a) not in unread]
    # scan-level gaps
    if live_read:
        if si.facts["temperature"].used_value is None:
            scan_gaps.append(_Gap("temperature", "no temperature in the files or saved; the fit uses the "
                                  "zero-temperature limit", scan, fallback=T_FALLBACK, order=(pos, "")))
        if not slit_given:
            label = _slit_label(cx, flags) if current else None
            scan_gaps.append(_Gap("slit", label or SLIT_PENDING, scan,
                                  fallback=label, state="fallback" if label else "pending", order=(pos, "")))
        if pending:
            first = next(iter(sorted(pending)))
            scan_gaps.append(_Gap("calibration_pending", uses_of[first]["calibrate"].reason or "", scan, order=(pos, "")))
        sc = imp.get("sidecar") or {}
        if sc.get("state") in ("missing", "unreadable"):
            scan_gaps.append(_Gap("sidecar", sc.get("reason") or f"the .var sidecar is {sc['state']}", scan,
                                  fallback=NO_SIDECAR, order=(pos, "")))
        if si.gpi is not None and not si.gpi.accepted:
            scan_gaps.append(_Gap("gpi_rejected", si.gpi.problem or "the gpi file was not used", scan,
                                  file=si.gpi.file, order=(pos, "")))
        if si.named_dropped is not None:
            scan_gaps.append(_Gap("gpi_curves_dropped", si.named_dropped, scan, order=(pos, "")))
        if any(fi.disagreement for fi in si.facts.values()):
            disagree.add(scan)
            scan_gaps.append(_Gap("disagreement", "; ".join(
                f"{f}: the file has {fi.file_value}, {fi.user_value} is saved and used" for f, fi in si.facts.items()
                if fi.disagreement), scan, order=(pos, "")))
        if failed:
            scan_gaps.append(_Gap("calibration_failed", f"tabulated calibration failed for this scan ({prev['error']})",
                                  scan, order=(pos, "")))
    if si.excluded is not None:
        scan_gaps.append(_Gap("declared", _declared_reason("scan excluded", si.excluded), scan, order=(pos, "")))
    gaps.extend(scan_gaps)

    # readiness of each analyzer, then of the scan
    marks_of = {a: marks.get(f"{scan}:{a}") for a in uses_of}
    analyzers: dict[str, AnalyzerReadiness] = {}
    for a, u in uses_of.items():
        m = marks_of[a]
        running = bool(m and m.running)
        declared = si.excluded is not None or a in si.dead      # analysis cannot start here; the declaration is what to reverse
        changed = () if declared else tuple(x for x in ((m.changed_fast, m.changed_deep) if m else ()) if x is not None)
        can = None if running else u["calibrate"].state not in _BLOCKING
        an_can[(scan, a)] = can
        scan_kinds = [(g.kind, g.state) for g in scan_gaps if g.kind in ("temperature", "slit", "sidecar")]
        action = "wait" if running else _pick(own.get(a, []) + scan_kinds, a in pending)
        curve, curve_kind, curve_reason = _curve_in_force(a, si, cx)
        analyzers[a] = AnalyzerReadiness(f"{scan}:{a}", a, u, can, action, running, changed,
                                         curve, curve_kind, curve_reason)
        if running:
            running_scans.add(scan)
            gaps.append(_Gap("running", "analysis is running on this analyzer", scan, a, order=(pos, a)))
        if changed:
            gaps.append(_Gap("values_changed", "; ".join(changed), scan, a, order=(pos, a)))
    # an analyzer declared dead, or with no file, has nothing to fit: it must not hold the whole scan
    nofile = {a for a, v in left_out.items() if ((v or {}).get("cause") or "no file") == "no file"}
    pool = [a for a in analyzers if a not in si.dead and a not in nofile] or list(analyzers)
    uses = {use: _worst([analyzers[a].uses[use] for a in pool]) for use in USES} if analyzers else \
        {use: _use("held", "no analyzer was read") for use in USES}
    can = _can_start([analyzers[a].can_start for a in analyzers]) if analyzers else False
    every = [ks for ls in own.values() for ks in ls] + [(g.kind, g.state) for g in scan_gaps]
    action = "wait" if can is None else _pick(every, bool(pending))
    return ScanReadiness(scan, analyzers, uses, can, action)


# ---------------------------------------------------------------------------------------------------------
# groups
# ---------------------------------------------------------------------------------------------------------
def _names(names: list[str]) -> str:
    if len(names) > 6:
        return f"{len(names)} analyzers"
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _summary(kind: str, title: str, items: list[_Gap], n_scans: int) -> str:
    target = KINDS[kind].target
    if target == "scan_or_analyzer":
        target = "analyzer" if any(g.analyzer for g in items) else "scan"
    if target == "analyzer":
        names = sorted({g.analyzer for g in items if g.analyzer})
        return f"{title}: analyzer{'s' if len(names) != 1 else ''} {_names(names)} on {_plural(n_scans, 'scan')}"
    if target == "scan":
        return f"{title}: {_plural(n_scans, 'scan')}"
    if target == "experiment":
        return f"{title}: {items[0].reason}"
    return f"{title}: {_plural(len(items), 'file' if target == 'file' else 'line')}"


def _groups(gaps: list[_Gap], marks, scans_ready: Mapping[str, ScanReadiness], an_can: dict, disagree: set,
            running_scans: set, n_rows: int) -> tuple[AttentionGroup, ...]:
    any_scan = _can_start([s.can_start for s in scans_ready.values()])
    out = []
    for kind in KIND_ORDER:
        mine = sorted((g for g in gaps if g.kind == kind), key=lambda g: g.order)
        if not mine:
            continue
        spec = KINDS[kind]
        items = []
        can = []
        for g in mine:
            m = marks.get(f"{g.scan}:{g.analyzer}") if g.analyzer else None
            items.append(AttentionItem(
                scan=g.scan, analyzer=g.analyzer, file=g.file, reason=g.reason, fallback=g.fallback,
                running=bool(m.running) if m else (g.analyzer is None and g.scan in running_scans),
                values_disagree=g.scan in disagree,
                values_changed=tuple(x for x in ((m.changed_fast, m.changed_deep) if m else ()) if x is not None)))
            if g.analyzer:
                can.append(an_can.get((g.scan, g.analyzer)))
            elif g.scan:
                can.append(scans_ready[g.scan].can_start)
            else:
                can.append(False if kind == "files_not_read" else any_scan)
        n_scans = n_rows if kind == "resolution_folder" else len({g.scan for g in mine if g.scan})
        state = "running" if kind == "running" else max((g.state for g in mine), key=_SEVERITY.__getitem__)
        analyzers = tuple(sorted({g.analyzer for g in mine if g.analyzer})) \
            if spec.target in ("analyzer", "scan_or_analyzer") else ()
        can_start = None if kind == "running" else _can_start(can)
        out.append(AttentionGroup(
            kind=kind, title=spec.title, summary=_summary(kind, spec.title, mine, n_scans), uses=spec.uses,
            state=state, action="wait" if can_start is None and all(i.running for i in items) else spec.action,
            can_start=can_start,
            fallback_in_use=any(g.fallback for g in mine), n_scans=n_scans, analyzers=analyzers, items=tuple(items)))
    return tuple(out)


def _damaged(sett, read, index) -> list[store.Damaged]:
    found = [*sett.damaged, *read.damaged, *index.damaged]
    return sorted(found, key=lambda d: (d.file != store.SETTINGS, d.line))


# ---------------------------------------------------------------------------------------------------------
# the two reads
# ---------------------------------------------------------------------------------------------------------
def evaluate(root, *, marks: Mapping[str, UnitMarks] | None = None) -> NeedsAttention:
    """Readiness per scan, analyzer and use, and the grouped needs-attention list. Writes nothing, starts
    nothing and never raises: what it cannot read gives ``evaluated=False`` with the reason in ``note``."""
    root = Path(root)
    try:
        return _evaluate(root, marks or {})
    except Exception as exc:  # noqa: BLE001 — a read surface degrades with its reason, and logs it
        log.exception("readiness: could not be computed for %s", root)
        return NeedsAttention((), {}, False, f"readiness could not be computed ({type(exc).__name__}: {exc})")


def _not_evaluated(note: str) -> NeedsAttention:
    return NeedsAttention((), {}, False, note)


def _evaluate(root: Path, marks: Mapping[str, UnitMarks]) -> NeedsAttention:
    if not (root / POLICY_FILE).exists():
        return _not_evaluated(f"not an ISAR workspace (no {POLICY_FILE})")
    card = _card(root)
    if not scans.applies(card):
        return _not_evaluated(NOT_BL43)
    try:
        rows, files = _record(root)
    except ValueError as exc:
        log.warning("readiness: the import record is unreadable (%s)", exc)
        return _not_evaluated(str(exc))
    sett = settings.current_settings(root)
    read = store.read_all(root, store.ENTRIES)
    index = inputs.index_entries(read.entries, lines=read.lines)
    damaged = _damaged(sett, read, index)
    if not rows and not files and not damaged:
        return _not_evaluated("nothing imported yet")
    check, check_error = _check(root)
    cidx = check.index() if check else CurveIndex(())
    stale = _stale(sett.resolution_folder, check, check_error)
    if check is None:
        folder_problem = check_error or NOT_CHECKED
    elif check.problem or check.folder is None:
        folder_problem = check.problem or curves_mod.NO_FOLDER
    elif not check.curves:
        folder_problem = f"the resolution folder {check.folder} holds no resolution curves"
    else:
        folder_problem = None
    if stale:
        folder_problem = None           # a folder saved since the last check is pending, never this hold
    state = card.state.tabulated_calibration
    gpi_stale = _stale(sett.gpi_folder, _gpi_check(root))
    gpi_entries = gpi_mod.entries_in_force(root)

    gaps: list[_Gap] = []
    an_can: dict = {}
    disagree: set = set()
    running_scans: set = set()
    ready: dict[str, ScanReadiness] = {}
    order = sorted(rows, key=lambda k: scan_order(rows, k))
    # a refused file lost an identity clash: it belongs to another set and never feeds an existing row
    unread = {p: rec for p, rec in files.items() if (rec or {}).get("class") == "failed"}
    for pos, scan in enumerate(order):
        ready[scan] = _scan(scan, rows[scan], pos, index, state, check, cidx, folder_problem, stale, marks,
                            gaps, an_can, running_scans, disagree, unread, gpi_entries, root / "data")
    if folder_problem is not None and rows:
        gaps.append(_Gap("resolution_folder", folder_problem))
    if gpi_stale and rows:
        gaps.append(_Gap("gpi_folder_changed", GPI_FOLDER_CHANGED))
    for path in sorted(files):
        rec = files[path] or {}
        if rec.get("class") in ("failed", "refused"):
            gaps.append(_Gap("files_not_read", rec.get("reason") or rec["class"], file=path, order=(path,)))
    for d in damaged:
        gaps.append(_Gap("damaged_lines", f"{d.file} line {d.line} skipped: {d.why}", file=d.file,
                         fallback=LINE_SKIPPED, order=(d.file != store.SETTINGS, d.line)))
    return NeedsAttention(_groups(gaps, marks, ready, an_can, disagree, running_scans, len(rows)), ready, True, None)


def _summary_of_curves(root: Path, card: Card, saved, check, check_error: str | None) -> CurveSummary:
    if saved is not None:
        folder, source, beside = saved.path, saved.source, saved.found_beside
    elif check is not None and check.source != "setting":      # the card's folder as the last check found it
        folder, source, beside = check.folder, check.source, None
    else:
        path, _pattern, source = curves_mod.resolution_location(card, root)
        folder, beside = (None if path is None else str(path)), None
    stale = _stale(saved, check, check_error)
    if stale:
        check = None        # the last check is of another folder: its figures are not the saved folder's
    problem = check_error
    if check is not None:
        problem = check.problem
    elif problem is None and folder is None:
        problem = "no resolution folder is saved"
    return CurveSummary(
        folder=folder, source=source, found_beside=beside,
        n_curves=0 if check is None else len(check.curves),
        analyzers=() if check is None else tuple(sorted({c.detector for c in check.curves})),
        settings=() if check is None else tuple(sorted({c.setting for c in check.curves if c.setting})),
        problem=problem, other_folders=() if check is None else check.other_folders,
        checked_at=None if check is None else check.checked_at, check_stale=stale)


def import_summary(root) -> ImportSummary:
    """What the imports read and did not, and where the curves were found. Writes nothing, never raises."""
    root = Path(root)
    try:
        return _import_summary(root)
    except Exception as exc:  # noqa: BLE001 — a read surface degrades with its reason, and logs it
        log.exception("readiness: the import summary could not be computed for %s", root)
        return ImportSummary(0, 0, (), (), {}, None, None, f"{type(exc).__name__}: {exc}")


def _import_summary(root: Path) -> ImportSummary:
    if not (root / POLICY_FILE).exists():
        return ImportSummary(0, 0, (), (), {}, None, None, f"not an ISAR workspace (no {POLICY_FILE})")
    card = _card(root)
    if not scans.applies(card):
        return ImportSummary(0, 0, (), (), {}, None, None)
    check, check_error = _check(root)
    curve = _summary_of_curves(root, card, settings.current_settings(root).resolution_folder, check, check_error)
    try:
        rows, files = _record(root)
    except ValueError as exc:
        log.warning("readiness: the import record is unreadable (%s)", exc)
        return ImportSummary(0, 0, (), (), {}, curve, None, str(exc))
    sets, unread = [], []
    for scan in sorted(rows, key=lambda k: scan_order(rows, k)):
        row = rows[scan]
        imp = row.get("import")
        if imp:
            left = {a: (v or {}).get("cause") or "no file" for a, v in (imp.get("left_out") or {}).items()}
            sc = imp.get("sidecar") or {}
            state = sc.get("state") or "read"
            if left or state in ("missing", "unreadable"):
                sets.append(IncompleteSet(scan, row.get("set"), dict(sorted(left.items())), state, sc.get("reason")))
            for a, cause in sorted(left.items()):
                if cause != "no file":
                    name = (imp["left_out"][a] or {}).get("file") or f"{row.get('set')}_{a}"
                    unread.append(FileNotRead(name, "left_out", cause, scan, a))
    for path, rec in files.items():
        rec = rec or {}
        unread.append(FileNotRead(path, rec.get("class", "unrecognised"), rec.get("reason") or "", None, None))
    unread.sort(key=lambda f: (f.path, f.analyzer or ""))
    return ImportSummary(
        n_scans=len(rows), n_analyzers=sum(len(r.get("analyzers") or {}) for r in rows.values()),
        incomplete_sets=tuple(sets), files_not_read=tuple(unread),
        files_not_read_by_kind=dict(sorted(Counter(f.kind for f in unread).items())), curves=curve,
        last_import_at=max((r["imported_at"] for r in rows.values() if r.get("imported_at")), default=None),
        gpi=_gpi_check(root))
