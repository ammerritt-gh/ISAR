"""The read facade over inter-spectra fitting (C-gui-reads, C-authority, C-preview). Qt-free, read-only.

This module never writes (AGENTS.md hard rules 6 and 7). Its reads sit on the authority resolver, the one rule
for the fit in use. The GUI and the notebooks read through here.
"""
from __future__ import annotations

import hashlib
import logging
import math
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from isar.analyze.coupled.linking import Change, LinkResult, Marker, TieGroup, fit_stamp
from isar.analyze.coupled.settings import InterSettings
from isar.intake import gpi, inputs, scans as intake_scans, store
from isar.intake.inter_spectra import FAST, InterUser, read_inter_settings, read_user
from isar.schema.card import load_policy, load_state
from isar.session.inter_spectra import authority, records
from isar.session.inter_spectra.authority import Stale
from isar.session.inter_spectra.graph import anchored_scope, build_graph
from isar.session.inter_spectra.layout import grid_for
from isar.session.inter_spectra.nodes import read_nodes
from isar.session.inter_spectra.preview import Q_NO_LAYOUT, preview_anchored, preview_simultaneous
from isar.session.inter_spectra.q_provider import INVALID_CELL, NO_HKL, q_coordinates
from isar.session.inter_spectra.records import ELIGIBLE
from isar.session.inter_spectra.rings import plan_anchored
from isar.session.inter_spectra.schedule import plan_simultaneous
from isar.session.state import load_shared
from isar.session.workspace import POLICY_NAME, SESSION_NAME, STATE_NAME

from .views import FitView

log = logging.getLogger(__name__)
Severity = Literal["info", "warn", "serious"]    # info: a repair (neighbour-supported split); warn; serious: tension, unresolved identity, did not settle


@dataclass(frozen=True)
class FlagReason:
    code: str; severity: Severity; detail: str = ""     # detail: backend text with numbers, shown on hover


@dataclass(frozen=True)
class FlagInfo:
    severity: Severity; score: float | None; cost_of_coupling: float | None; link_mismatch: float | None
    reasons: tuple[FlagReason, ...]
    flagged: bool = False                 # on the review list: refit_flagged's rule, never inferred from severity


def is_flagged(flag: FlagInfo | None) -> bool:
    """The review list: the refit's stored decision, or a reason the facade adds whatever the score. A warn note
    such as release_fit_failed is listed, not flagged; an info reason is a repair (design, Flags)."""
    return flag is not None and flag.flagged


@dataclass(frozen=True)
class SpectrumInter:
    key: str; fit_id: str | None          # the authoritative fit ("fast" or a refit id), stale rule already applied; None: nothing to use
    in_use: bool                          # a refit is in use
    source: Literal["fast", "batch", "pick", "lock"] | None    # None exactly when fit_id is None
    label: str                            # "Fast solo" | "Batch 2 · sweep 3" | "No fit in use"
    flag: FlagInfo | None
    locked: bool; locked_fit_id: str | None; lock_stale: bool
    stale: bool                           # own refits exist but are stale, so the fast solo stands in
    stale_text: str | None                # the plain sentence for it ("its own inputs changed since the refit")


@dataclass(frozen=True)
class BatchSummary:
    batch_id: str; label: str; kind: str; direction: str | None; status: str
    n_total: int; n_done: int; n_failed: int; n_not_reached: int; anchors: tuple[str, ...]


@dataclass(frozen=True)
class InterOverview:
    revision: str; by_key: Mapping[str, SpectrumInter]; anchors: tuple[str, ...]
    settings: Mapping[str, object]; batches: tuple[BatchSummary, ...]


@dataclass(frozen=True)
class FitEntry:                           # completed fits only (rejected/failed are never offered)
    fit_id: str; label: str                                # "Fast solo" | "Batch 1 · ring 2" | "Batch 2 · sweep 3"
    batch_id: str | None; batch_label: str | None; stage_label: str | None    # "ring 2" | "sweep 3"
    changed: bool                                          # False: an unchanged refit (labelled in the flat dropdown)
    authoritative: bool; picked: bool; locked: bool; stale: bool; stale_text: str | None
    flag: FlagInfo | None; why_changed: tuple[FlagReason, ...]
    conditional_errors: bool                               # True for every refit; False for Fast solo
    errors_available: bool                                 # False: covariance failed -> "unavailable"


@dataclass(frozen=True)
class BatchRequest:                      # built by the GUI
    kind: Literal["anchored", "simultaneous"]
    direction: Literal["q", "T", "qT"] | None    # anchored only
    anchors: tuple[str, ...]             # anchored only
    selection: tuple[str, ...]           # simultaneous only


@dataclass(frozen=True)
class BatchPreview:
    can_start: bool; reason: str | None                    # reason is a plain sentence when can_start is False
    members: tuple[str, ...]                               # the scope's spectrum keys, sorted
    line: str                                              # "2 anchors · spread in q · 56 spectra · 6 rings", shown verbatim
    notes: tuple[str, ...]


# ---------------------------------------------------------------------------
# the plain words, in one table (the tests and the GUI read the same sentences)
# ---------------------------------------------------------------------------
STALE_WORDS = {"inputs_changed": "its own inputs changed since the refit",
               "neighbour_inputs_changed": "inputs of {} changed since the refit"}
NO_ANCHOR = "choose at least one anchor first"
NO_DIRECTION = "choose a direction first (q, T, or q and T)"
NO_SELECTION = "select at least two spectra first"
NO_FIT = "No fit in use"
NO_Q = "spreading in q needs each analyzer's accepted hkl; none is accepted for this scan"
NOT_COUPLED = (NO_HKL, "unknown spectrum")       # provider reasons the preview does not count as a note
_SEV = {"info": 0, "warn": 1, "serious": 2}


def _stale_text(stale: Stale | None) -> str | None:
    return None if stale is None else STALE_WORDS[stale.code].format(stale.unit)


@dataclass(frozen=True)
class _Ctx:
    """One consistent read of the log, the user values and the fingerprints, and the resolver's answers."""
    root: Path
    log: records.InterLog
    user: InterUser
    solo: frozenset[str]                  # spectrum keys with a stored fast solo fit
    now: Mapping[str, str | None]
    res: Mapping[str, authority.Resolution]
    lines: Mapping[str, records.RefitLine]
    stages: Mapping[tuple, list]


def _inputs_now(root: Path, units) -> dict[str, str | None]:
    if not units:
        return {}
    read = store.read_all(root, store.ENTRIES)
    return records.inputs_now_from(intake_scans.read_scans(root), inputs.index_entries(read.entries, lines=read.lines),
                                   load_state(root / STATE_NAME).tabulated_calibration, sorted(units),
                                   gpi=gpi.entries_in_force(root), data_dir=root / "data")


def _solo_keys(root: Path) -> frozenset[str]:
    try:
        session = load_shared(root / SESSION_NAME)
    except FileNotFoundError:
        return frozenset()
    return frozenset(k for k, e in session.scans.items() if authority.has_fast_solo(e, k))


def _load(root, only: str | None = None) -> _Ctx:
    """Everything the readers share. ``only``: just that spectrum (and the neighbours its refits used)."""
    root = Path(root)
    log, user = records.read_log(root), read_user(root)
    refits = [r for r in log.refits if r.status in ELIGIBLE and (only is None or r.unit == only)]
    needed = {r.unit for r in refits} | {c.unit for r in refits for c in r.conditioned_on}
    now = _inputs_now(root, needed)
    keys = _solo_keys(root)             # also warms the shared ledger for the preview and history reads that follow
    solo = keys if (refits or user.picks or user.locks or only) else frozenset()
    if only is None:
        res = authority.resolve_all(log, user, now, solo)
    else:
        res = {only: authority.resolve(only, refits=refits, pick=user.picks.get(only), lock=user.locks.get(only),
                                       has_fast=only in solo, inputs_now=now)}
    stages: dict[tuple, list] = {}
    for st in log.stages:
        stages.setdefault((st.batch, st.stage, st.index), []).append(st)
    return _Ctx(root, log, user, solo, now, res, {r.fit: r for r in log.refits}, stages)


def _added_reasons(log: records.InterLog, stages: Mapping[tuple, list], line: records.RefitLine) -> list[FlagReason]:
    """The reasons the facade adds on its own, whatever the refit's score: a seam disagreement its ring or sweep
    measured, a batch that did not settle, and a cumulative T drift its batch's end measured along a branch
    through this spectrum. Each puts the refit on the review list."""
    out = []
    for st in stages.get((line.batch, line.stage, line.index), ()):
        for seam in st.seams:
            if line.unit in (seam.get("a"), seam.get("b")):
                f = seam.get("flag") or {}
                out.append(FlagReason(f.get("code", "seam_disagreement"), f.get("severity", "warn"), f.get("text", "")))
    info = log.batches.get(line.batch)
    if info is not None and info.end and info.end.get("state") == "did_not_settle":
        out.append(FlagReason("did_not_settle", "serious", info.end.get("reason") or ""))
    drift = ((info.end or {}).get("drift") if info is not None else None) or {}
    segs = [s for s in drift.get("segments") or () if s.get("flagged") and line.unit in (s.get("units") or ())]
    if segs:
        sev = ((drift.get("flags") or {}).get("t_drift_cumulative") or {}).get("severity", "warn")
        out.append(FlagReason("t_drift_cumulative", sev, "; ".join(
            f"branch {s.get('branch')}: {s.get('dE', 0.0):+.2f} meV over {s.get('dT', 0.0):.0f} K (U {s.get('U', 0.0):.3g})"
            for s in segs)))
    return out


def refit_flagged(log: records.InterLog, line: records.RefitLine) -> bool:
    """Whether one stored refit is on the review list (a stale lock is added only for the fit in use, in _flag)."""
    stages: dict[tuple, list] = {}
    for st in log.stages:
        stages.setdefault((st.batch, st.stage, st.index), []).append(st)
    return bool(line.flag and line.flag.flagged) or bool(_added_reasons(log, stages, line))


def _flag(c: _Ctx, line: records.RefitLine, payload: dict | None, *, stale_locked: bool = False) -> FlagInfo | None:
    """The refit's own flag, plus the seam disagreement its ring or sweep measured, plus a batch that did not
    settle, plus a stale locked fit. A spectrum on its solo fit is never flagged here. The score F = R + max U
    (C-refit): cost_of_coupling is R and link_mismatch the largest U, both from the stored refit."""
    reasons = [FlagReason(r["code"], r["severity"], r.get("text", "")) for r in (line.flag.reasons if line.flag else ())]
    added = _added_reasons(c.log, c.stages, line)
    if stale_locked:
        added.append(FlagReason("stale_locked_fit", "serious", ""))
    reasons += added
    unique = list(dict.fromkeys(reasons))
    flagged = bool(added) or bool(line.flag and line.flag.flagged)
    if not unique and not flagged:
        return None
    severity = max((r.severity for r in unique), key=_SEV.__getitem__, default="warn")
    came_out = (payload or {}).get("came_out") or {}
    gaps = [float(u) for u in (came_out.get("U") or {}).values() if u is not None]
    return FlagInfo(severity, line.flag.score if line.flag else None, came_out.get("R"), max(gaps, default=None),
                    tuple(unique), flagged)


def _label(c: _Ctx, r: authority.Resolution) -> str:
    if r.source == "fallback":
        chosen = authority.fit_label(c.lines.get(r.chosen), c.log.batches)
        shown = "Fast solo" if r.fit == FAST else NO_FIT
        return f"{shown} ({chosen} is out of date: {_stale_text(r.stale)})"
    if r.fit is None:
        return NO_FIT
    base = authority.fit_label(c.lines.get(r.fit) if r.fit != FAST else None, c.log.batches)
    return {"pick": f"Your pick: {base}", "lock": f"Locked: {base}"}.get(r.source, base)


def _spectrum(c: _Ctx, unit: str, r: authority.Resolution) -> SpectrumInter:
    line = c.lines.get(r.fit) if r.fit not in (None, FAST) else None
    lock_stale = r.locked and r.stale is not None
    source = None if r.fit is None else "fast" if r.source in ("fast_solo", "fallback") else r.source
    return SpectrumInter(
        key=unit, fit_id=r.fit, in_use=line is not None, source=source,
        label=_label(c, r),
        flag=None if line is None else _flag(c, line, records.read_payload(c.root, line), stale_locked=lock_stale),
        locked=r.locked, locked_fit_id=r.lock_fit, lock_stale=lock_stale, stale=r.source == "fallback",
        stale_text=_stale_text(r.stale))


def _summary(c: _Ctx, info: records.BatchInfo) -> BatchSummary:
    end = info.end
    last = next((s for s in reversed(c.log.stages) if s.batch == info.batch), None)
    if end is not None:
        done, failed, missed = int(end.get("done") or 0), int(end.get("failed") or 0), len(end.get("not_reached") or ())
    elif last is not None:
        p = last.progress
        done, failed, missed = int(p.get("done", 0)), int(p.get("failed", 0)), int(p.get("not_reached", 0))
    else:
        done = failed = missed = 0
    return BatchSummary(info.batch, f"Batch {info.number}", info.batch_kind, info.scope.get("direction"),
                        end["state"] if end else "running", len(info.scope.get("members") or ()), done, failed,
                        missed, info.anchors)


def inter_overview(root) -> InterOverview:
    c = _load(root)
    settings = InterSettings.from_values(c.user.settings).to_values()     # every named setting, defaults filled
    if not c.log.batches and not c.user.max_seq:
        return InterOverview("0", {}, (), settings, ())
    by_key = {u: _spectrum(c, u, r) for u, r in c.res.items()}
    batches = tuple(_summary(c, b) for b in sorted(c.log.batches.values(), key=lambda b: b.number))
    revision = hashlib.sha256(repr((by_key, c.user.anchors, settings, batches)).encode("utf-8")).hexdigest()[:16]
    return InterOverview(revision, by_key, c.user.anchors, settings, batches)


def inter_history(root, key: str) -> tuple[FitEntry, ...]:
    """The fast solo fit, then the spectrum's completed refits by batch number and log order. ``()`` when it has
    neither. Rejected and failed attempts are in the log, not here."""
    c = _load(root, only=key)
    r = c.res[key]
    pick, lock = c.user.picks.get(key), c.user.locks.get(key)
    locked_fit = r.lock_fit if r.locked else None
    out = []
    if key in c.solo:
        out.append(FitEntry(FAST, "Fast solo", None, None, None, True, r.fit == FAST, pick is not None and pick.fit == FAST,
                            locked_fit == FAST, False, None, None, (), False, True))
    number = {b: i.number for b, i in c.log.batches.items()}
    refits = sorted((x for x in c.log.refits if x.unit == key and x.status in ELIGIBLE),
                    key=lambda x: number.get(x.batch, 0))                  # stable: log order inside a batch
    for x in refits:
        stale = authority.stale_of(x, c.now)
        payload = records.read_payload(c.root, x)
        why = (FlagReason("why_changed", "info", x.why_changed),) if x.why_changed else ()
        out.append(FitEntry(
            x.fit, authority.fit_label(x, c.log.batches), x.batch, f"Batch {number.get(x.batch, x.batch)}",
            f"{x.stage} {x.index}", x.status != "unchanged", r.fit == x.fit, pick is not None and pick.fit == x.fit,
            locked_fit == x.fit, stale is not None, _stale_text(stale),
            _flag(c, x, payload, stale_locked=locked_fit == x.fit and stale is not None), why, True,
            payload is not None and (payload.get("came_out") or {}).get("errors") == "conditional"))
    return tuple(out)


def _solo_view(ws, unit: str):
    for sv in ws.scans:
        if sv.scan_id == unit:
            return next(iter(sv.detectors.values()), None)
    return None


def _view(ws, log: records.InterLog, lines: Mapping[str, records.RefitLine], unit: str, fit: str):
    """``fit`` of a spectrum drawn from its solo outcome and the stored overlay, with its label; ``lines`` maps each
    fit id of ``log`` to its line. ``(None, label)`` when the fit is not recorded or cannot be drawn."""
    solo = _solo_view(ws, unit)
    if fit == FAST:
        return solo, "Fast solo"
    line = lines.get(fit)
    label = authority.fit_label(line, log.batches)
    if line is None or line.unit != unit or line.status not in ELIGIBLE or solo is None:
        return None, label
    payload = records.read_payload(ws.root, line)
    if payload is None or payload.get("overlay") is None:
        return None, label
    return FitView(row=None, outcome=authority.overlay_outcome(solo._outcome, payload["overlay"]),
                   scan_number=solo.scan_number, detector_id=solo.detector_id, unit_id=unit), label


def fit_view_and_label(ws, unit: str, fit: str = "authoritative") -> tuple[FitView | None, str]:
    """The drawable view of ``fit`` (a fit id, ``"fast"``, or ``"authoritative"`` for the one in use) of a spectrum
    and its plain label; ``(None, label)`` when the fit is not recorded or cannot be drawn."""
    if fit == "authoritative":
        fit = _load(ws.root, only=unit).res[unit].fit
    if fit is None:
        return None, ""
    log = records.read_log(ws.root)
    return _view(ws, log, {r.fit: r for r in log.refits}, unit, fit)


def fit_views(ws, fits: Mapping[str, str]) -> dict[str, tuple[FitView | None, str]]:
    """``fit_view_and_label`` for many spectra, each with a concrete fit id: the log is read once for all of them."""
    if not fits:
        return {}
    log = records.read_log(ws.root)
    lines = {r.fit: r for r in log.refits}
    return {unit: _view(ws, log, lines, unit, fit) for unit, fit in fits.items()}


def fit_view_of(ws, unit: str, fit: str = "authoritative") -> FitView | None:
    return fit_view_and_label(ws, unit, fit)[0]


def _no_q_points(grid, qc) -> str:
    """Why a q batch has no point at all: the instrument has no analyzer layout, a scan's epoch is not a cell, or no
    analyzer has an accepted hkl."""
    if grid is None:
        return Q_NO_LAYOUT
    if INVALID_CELL in qc.reasons.values():
        return INVALID_CELL
    return NO_Q


def batch_preview(root, req: BatchRequest) -> BatchPreview:
    """What a batch would do, before Start. A pure read over the graph of S1U4 and the q provider."""
    root = Path(root)
    if req.kind == "anchored":
        if not req.anchors:
            return BatchPreview(False, NO_ANCHOR, (), "", ())
        if req.direction not in ("q", "T", "qT"):
            return BatchPreview(False, NO_DIRECTION, (), "", ())
    elif len(req.selection) < 2:
        return BatchPreview(False, NO_SELECTION, (), "", ())
    user, settings = read_user(root), read_inter_settings(root)
    ns = read_nodes(root)
    policy = load_policy(root / POLICY_NAME)
    grid = grid_for(policy.plugin) if policy is not None else None
    locks = {u for u, ls in user.locks.items() if ls.locked}
    notes: list[str] = []
    if req.kind == "anchored":
        scope, kinds = anchored_scope(req.anchors, req.direction, ns)
    else:
        scope, kinds = frozenset(k for k in req.selection if k in ns.nodes), frozenset({"q", "T"})
        if len(scope) < len(set(req.selection)):
            notes.append(f"{len(set(req.selection)) - len(scope)} selected spectra are unknown to the import record")
    qc = q_coordinates(root, scope)
    graph = build_graph(ns.nodes, ns, scope=scope, kinds=kinds, grid=grid,
                        coords=qc.points if "q" in kinds else {}, collinearity_tol=settings.collinearity_tol,
                        q_reasons=qc.reasons)
    locked_here = locks & set(scope)
    if req.kind == "anchored":
        res = _load(root).res
        stale_locks = {u for u in locked_here if u in res and not res[u].conditions}
        pv = preview_anchored(graph, plan_anchored(graph, req.anchors, locked_here, stale_locks), req.direction)
    else:
        pv = preview_simultaneous(graph, plan_simultaneous(graph, locked_here))
    for why, n in sorted(Counter(v for v in qc.reasons.values() if v not in NOT_COUPLED).items()):
        notes.append(f"{why}: {n} spectra")
    can_start, reason = pv.can_start, pv.blocked
    if req.kind == "anchored" and req.direction == "q" and not qc.points:
        can_start, reason = False, _no_q_points(grid, qc)
    return BatchPreview(can_start, None if can_start else reason, tuple(sorted(scope)), pv.text, (*pv.notes, *notes))


# ---------------------------------------------------------------------------
# saved mode links (C-link-view): each spectrum's newest record, judged against the fits in use now
# ---------------------------------------------------------------------------
LINK_WORDS = {
    "stale": "made from fits that have changed since: run Link modes again",
    "unnamed": "saved before its phonons were named: run Link modes again",
    "change": "{} differs from neighbours", "at_cap": "at the width cap (wants to be broader)",
    "unlinked": "no link to a neighbour", "contradiction": "links contradict each other",
    "add": "{} is expected near {:.1f} meV here, but no mode of this spectrum follows it",
    "split": "Mode {} may be two branches, {} and {}; not settled", "drop": "Mode {} follows no neighbour",
    "swap": "{} and {} are nearly tied at this crossing", "competition": "Mode {} may follow {} or {}; nearly tied",
}
MarkerName = Literal["change", "at_cap", "unlinked", "contradiction"]
_MARK_GROUP = {"change": 2, "at_cap": 2, "contradiction": 2, "unlinked": 3}          # design 2.4
_MARK_SEVERITY = {"change": "warn", "at_cap": "info", "unlinked": "warn", "contradiction": "serious"}


@dataclass(frozen=True)
class MarkerView:
    kind: MarkerName; group: Literal[2, 3]; mode: int | None; quantity: Literal["energy", "width", "intensity"] | None
    label: str; words: str; severity: Severity


@dataclass(frozen=True)
class ChangeView:                                    # a listed change (design 2.2): reported, not applied
    kind: Literal["add", "split", "drop"]; mode: int | None; names: tuple[str, ...]; energy: float | None; words: str


@dataclass(frozen=True)
class TieView:
    id: str; names: tuple[str, ...]; units: tuple[str, ...]; words: str


@dataclass(frozen=True)
class UnitLinks:
    unit: str; link_id: str; at: str                 # the record it came from ("" for no record)
    stale: bool; stale_text: str | None              # the record is out of date
    current: bool                                    # this spectrum's own fit in the record is still the one in use
    fit_id: str | None                               # the fit in use now; names index its modes
    names: tuple[str, ...]                           # per mode, its phonon's stored name (B<n>; phonon_label shows P<n>); () unless current
    peak_E: tuple[float, ...]                        # per mode, the E* the link matched on (meV); () unless current
    unresolved: tuple[tuple[str, ...], ...]          # per mode, both names of a split candidate, else ()
    settled: tuple[bool, ...]                        # () unless current
    markers: tuple[MarkerView, ...]                  # () when stale or not current
    changes: tuple[ChangeView, ...]                  # () when stale or not current
    ties: tuple[TieView, ...]                        # () when stale or not current


def phonon_label(name: str) -> str:
    """The label every surface shows for a phonon: its stored name ``B<n>`` as ``P<n>``."""
    return "P" + name[1:]


def link_time(stamp: str) -> str:
    """A saved link's time (stored as ISO) in local time, 'YYYY-MM-DD HH:MM', as the operator reads it."""
    return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class BranchView:
    name: str; id: str; members: tuple[tuple[str, int], ...]     # (unit, 0-based mode index)


@dataclass(frozen=True)
class LinkView:
    revision: str                                    # "" exactly when there is nothing to show
    by_unit: Mapping[str, UnitLinks]                 # spectra a saved link covers; all others are absent
    branches: Mapping[str, BranchView]               # by name, over the records read


@dataclass(frozen=True)
class _LinkNow:
    fits: Mapping[str, str | None]                   # the fit in use now
    inputs: Mapping[str, str | None]                 # each unit's inputs hash now
    stamps: Mapping[str, str | None]                 # the fit in use's stamp (None: no fit, or its peaks unreadable)


def _session_scans(root: Path) -> Mapping:
    try:
        return load_shared(root / SESSION_NAME).scans
    except FileNotFoundError:
        return {}


def _in_use(c: _Ctx, scans: Mapping, unit: str) -> str | None:
    r = c.res.get(unit)                 # units with a refit, pick or lock; the rest are their solo fit
    if r is not None:
        return r.fit
    return FAST if authority.has_fast_solo(scans.get(unit), unit) else None


def _modes(rec: dict | None) -> list[tuple[float, float, float | None]] | None:
    """A fit's stored (omega0, Gamma, area) per peak; None when they cannot be read or are not sound (the session
    refuses such a spectrum with no token, so its record never names one)."""
    if not rec:
        return None
    try:
        modes = [(float(p["omega0"]), float(p["Gamma"]), p.get("area_stokes")) for p in rec.get("peaks") or ()]
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("a stored fit's peaks cannot be read: %s", exc)
        return None
    if not all(math.isfinite(o) and o > 0 and math.isfinite(g) and g > 0 for o, g, _ in modes):
        return None
    return modes


def _stamp_now(c: _Ctx, scans: Mapping, unit: str, fit: str | None, now: Mapping[str, str | None]) -> str | None:
    """The stamp the session gives a link of this fit (``session.inter_spectra.linking._in_use``)."""
    if fit is None:
        return None
    if fit == FAST:
        entry = scans.get(unit)
        rec = ((entry.detectors or {}).get(unit.rsplit(":", 1)[1]) or {}).get("fast_pass") if entry else None
        recorded = ((entry.provenance or {}).get("fit_inputs") or {}).get("hash") if entry else None
        inputs_ = recorded if now.get(unit) is not None and recorded != now[unit] else now.get(unit)
    else:
        line = c.lines.get(fit)
        payload = None if line is None else records.read_payload(c.root, line)
        rec = ((payload or {}).get("overlay") or {}).get("fast_pass")
        inputs_ = line.inputs if line is not None and line.inputs is not None else now.get(unit)
    modes = _modes(rec)
    return None if modes is None else fit_stamp(fit, inputs_, modes)


def _link_now(root: Path, units: Collection[str]) -> _LinkNow:
    c = _load(root)
    scans = _session_scans(root)
    now = {**c.now, **_inputs_now(root, [u for u in units if u not in c.now])}
    fits = {u: _in_use(c, scans, u) for u in units}
    return _LinkNow(fits, now, {u: _stamp_now(c, scans, u, fits[u], now) for u in units})


def _fresh(R: LinkResult, unit: str, now: _LinkNow) -> bool:
    """A spectrum's own entry of a record names the fit in use, its inputs and its stamp."""
    fit, h = R.made_from[unit]
    cur = now.inputs.get(unit)
    return (now.fits.get(unit) == fit and (h is None or cur is None or h == cur)
            and R.stamps.get(unit) == now.stamps.get(unit))


def _stale(R: LinkResult, now: _LinkNow) -> bool:
    """A record is out of date when a fit it names has changed, or a spectrum it left out now reads differently."""
    return (not all(_fresh(R, u, now) for u in R.made_from)
            or any(token != now.stamps.get(u) for u, (_, token) in R.excluded.items()))


def _marker(m: Marker) -> MarkerView:
    return MarkerView(m.kind, _MARK_GROUP[m.kind], m.mode, m.quantity, LINK_WORDS[m.kind].format(m.quantity), m.words,
                      _MARK_SEVERITY[m.kind])


def _change(ch: Change, names: Mapping[str, str]) -> ChangeView:
    if ch.kind == "add":
        p, name = ch.predictions[0], names[ch.branches[0]]
        return ChangeView("add", None, (name,), p.E, LINK_WORDS["add"].format(name, p.E))
    if ch.kind == "split":
        a, b = names[ch.branches[0]], names[ch.branches[1]]
        return ChangeView("split", ch.mode, (a, b), None, LINK_WORDS["split"].format(ch.mode + 1, a, b))
    return ChangeView("drop", ch.mode, (), None, LINK_WORDS["drop"].format(ch.mode + 1))


def _tie(g: TieGroup, names: Mapping[str, str]) -> TieView:
    a, b = names[g.branches[0]], names[g.branches[1]]
    if g.kind == "swap":
        return TieView(g.id, (a, b), g.units, LINK_WORDS["swap"].format(a, b))
    contested = next(ms[0] for ms in g.modes.values() if len(ms) == 1)     # the one mode on one side
    return TieView(g.id, (a, b), g.units, LINK_WORDS["competition"].format(contested + 1, a, b))


def _unit_links(line: records.LinkLine, R: LinkResult, unit: str, now: _LinkNow, stale: bool,
                names: Mapping[str, str]) -> UnitLinks:
    text, fit = (LINK_WORDS["stale"] if stale else None), now.fits.get(unit)
    if unit not in R.made_from or not _fresh(R, unit, now):
        return UnitLinks(unit, line.link, line.at, stale, text, False, fit, (), (), (), (), (), (), ())
    if unit not in R.lone:          # a record saved before lone phonons were named: every mode lacks its name
        return UnitLinks(unit, line.link, line.at, True, LINK_WORDS["unnamed"], False, fit, (), (), (), (), (), (), ())
    changes = R.changes.get(unit, ())
    unresolved = [() for _ in R.labels[unit]]
    for ch in changes:
        if ch.kind == "split":
            unresolved[ch.mode] = tuple(names[b] for b in ch.branches)
    return UnitLinks(
        unit, line.link, line.at, stale, text, True, fit,
        tuple(names[b] if b is not None else R.lone[unit][i] for i, b in enumerate(R.labels[unit])),
        tuple(R.peak_E[unit]), tuple(unresolved),
        tuple(R.settled[unit]),
        () if stale else tuple(_marker(m) for m in R.markers if m.unit == unit),
        () if stale else tuple(_change(ch, names) for ch in changes),
        () if stale else tuple(_tie(g, names) for g in R.ties if unit in g.units))


def link_view(root, units: Collection[str] | None = None) -> LinkView:
    """The saved mode links per spectrum: the newest record of its scan, read against the fits in use now. Reads only.

    ``units`` narrows the spectra. A record whose payload cannot be read covers nothing (the reader logs it).
    """
    root = Path(root)
    want = None if units is None else set(units)
    newest = records.newest_link_by_scan(records.read_links(root),
                                         None if want is None else {u.rsplit(":", 1)[0] for u in want})
    payloads: dict[str, LinkResult | None] = {}          # each record is read once
    for line in newest.values():
        if line.link not in payloads:
            payloads[line.link] = records.read_link_payload(root, line)
    read = {line.link: (line, payloads[line.link]) for line in newest.values() if payloads[line.link] is not None}
    if not read:
        return LinkView("", {}, {})
    now = _link_now(root, sorted({u for _, R in read.values() for u in (*R.made_from, *R.excluded)}))
    by_unit: dict[str, UnitLinks] = {}
    for scan, line in newest.items():
        if line.link not in read:
            continue
        _, R = read[line.link]
        names, stale = {b.id: b.name for b in R.branches}, _stale(R, now)
        for unit in (*R.made_from, *R.excluded):
            if unit.rsplit(":", 1)[0] == scan and (want is None or unit in want):
                by_unit[unit] = _unit_links(line, R, unit, now, stale, names)
    branches: dict[str, BranchView] = {}
    for _, R in read.values():
        for b in R.branches:
            old = branches.get(b.name)
            members = set(b.members) | (set(old.members) if old else set())
            branches[b.name] = BranchView(b.name, b.id, tuple(sorted(members)))
    if not by_unit and not branches:
        return LinkView("", {}, {})
    revision = hashlib.sha256(repr((sorted(by_unit.items(), key=lambda kv: kv[0]),
                                    sorted(branches.items(), key=lambda kv: kv[0]))).encode("utf-8")).hexdigest()[:16]
    return LinkView(revision, by_unit, branches)
