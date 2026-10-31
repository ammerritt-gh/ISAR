"""Which fit of a spectrum is in use, and what the readers see (C-authority). Pure; no I/O in ``resolve``.

The resolver is the one rule every reader uses, so the GUI, export and report cannot disagree. A user pick and a
machine acceptance are ordered on one causal clock, the intake store's ``seq`` (a pick has ``pick.seq``, an
acceptance ``intake_seq + 0.5``: it saw every pick with ``seq <= intake_seq`` and none above), never wall time.
Imports stdlib, ``records`` and ``isar.intake.inter_spectra`` at module level, so it loads no numpy;
``effective`` reads the fingerprints through ``records.inputs_now_from``, which imports numpy when it runs.
"""
from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from isar.intake.inter_spectra import FAST, InterUser, LockState, Pick, read_user
from isar.session.inter_spectra import records
from isar.session.inter_spectra.records import ELIGIBLE, InterLog, RefitLine

_log = logging.getLogger(__name__)

# The stored overlay's keys, redrawn in place of the solo fit's (C-authority, N3)
OVERLAY_KEYS = ("fast_pass", "modes", "model_manifest", "omega0", "best_label", "n_modes", "interval")
# The reconstruction context a refit was made with (calibration, resolution, temperature), saved in its overlay:
# a locked or older refit is drawn in its own context, never in a later solo fit's.
CONTEXT_KEYS = ("calib", "T")
# FitOutcome fields that state the solo fit's verdict or interval. Blanked in an overlaid outcome, because
# FitView._pick and _fast_pass_columns would present the solo verdict next to a refit's numbers.
SOLO_VERDICT_KEYS = ("verdict", "disposition", "confidence", "omega0_err", "omega0_profile", "hesse_interval",
                     "hesse_trusted", "bayes_interval", "bayes_ok", "model_interval", "model_robust",
                     "interval_reliable", "deep_pass", "candidates")


@dataclass(frozen=True)
class Stale:
    code: str; unit: str                  # "inputs_changed" (unit = the spectrum) | "neighbour_inputs_changed" (unit = the neighbour)


@dataclass(frozen=True)
class Resolution:
    unit: str
    fit: str | None            # the fit in use: "fast" | f-id | None (no solo fit and nothing chosen)
    source: str                # "fast_solo" | "batch" | "pick" | "lock" | "fallback"
    chosen: str | None         # what picks/acceptances/lock selected before the stale test ("fast" if nothing)
    locked: bool; lock_fit: str | None
    stale: Stale | None        # staleness of `chosen` (a refit); None for fast, or when it is not stale
    conditions: bool          # may this spectrum's fit condition a new fit? False only for a locked, stale fit
    notes: tuple[str, ...]     # dangling pick / lock naming a missing fit / inputs not judged


@dataclass(frozen=True)
class Effective:
    unit: str; outcome: dict; resolution: Resolution; label: str; line: RefitLine | None


def has_fast_solo(entry, unit: str) -> bool:
    """A stored fast solo fit: the ledger entry is ``fitted`` and carries its fast-pass record (file_fit's test)."""
    if entry is None or entry.status != "fitted":
        return False
    return bool((entry.detectors.get(unit.rsplit(":", 1)[1]) or {}).get("fast_pass"))


def stale_of(line: RefitLine, inputs_now: Mapping[str, str | None]) -> Stale | None:
    """Why a refit is out of date now: its own inputs first, else the first neighbour it was conditioned on."""
    own = inputs_now.get(line.unit)
    if own is not None and line.inputs is not None and own != line.inputs:
        return Stale("inputs_changed", line.unit)
    for c in line.conditioned_on:
        now = inputs_now.get(c.unit)
        if now is not None and c.inputs is not None and now != c.inputs:
            return Stale("neighbour_inputs_changed", c.unit)
    return None


def resolve(unit: str, *, refits: Sequence[RefitLine], pick: Pick | None, lock: LockState | None,
            has_fast: bool, inputs_now: Mapping[str, str | None]) -> Resolution:
    cands = {r.fit: r for r in refits if r.unit == unit and r.status in ELIGIBLE}
    if has_fast:
        cands[FAST] = None                                   # the solo fit: judged by the existing currentness
    notes: list[str] = []
    locked = bool(lock and lock.locked)
    lock_fit = lock.fit if locked else None
    chosen: str | None = None
    kind = "fast_solo"
    if locked:
        if lock_fit in cands:
            chosen, kind = lock_fit, "lock"
        else:
            notes.append(f"the lock names {lock_fit}, which is not a recorded fit")
    if chosen is None:
        events = []                                          # (clock, tiebreak, fit, kind)
        if pick is not None:
            if pick.fit in cands:
                events.append((float(pick.seq), 0, pick.fit, "pick"))
            else:
                notes.append(f"the pick names {pick.fit}, which is not a recorded fit")
        for pos, r in enumerate(r for r in refits if r.unit == unit and r.fit in cands):
            if r.accept is not None and r.accept.decision == "authoritative":
                events.append((r.accept.intake_seq + 0.5, pos + 1, r.fit, "batch"))
        if events:
            _, _, chosen, kind = max(events)
        elif has_fast:
            chosen, kind = FAST, "fast_solo"
    stale = None
    if chosen is not None and chosen != FAST:
        line = cands[chosen]
        stale = stale_of(line, inputs_now)
        if inputs_now.get(unit) is None or line.inputs is None:
            notes.append("its inputs are not judged")
    if stale is not None and kind == "lock":
        return Resolution(unit, chosen, "lock", chosen, True, lock_fit, stale, False, tuple(notes))
    if stale is not None:
        return Resolution(unit, FAST if has_fast else None, "fallback", chosen, locked, lock_fit, stale, True,
                          tuple(notes))
    return Resolution(unit, chosen, kind, chosen, locked, lock_fit, None, True, tuple(notes))


def resolve_all(log: InterLog, user: InterUser, inputs_now: Mapping[str, str | None],
                fast_units: Collection[str]) -> dict[str, Resolution]:
    """A resolution for every spectrum with an eligible refit, a pick or a lock; the others are their solo fit."""
    by_unit: dict[str, list[RefitLine]] = {}
    for r in log.refits:
        by_unit.setdefault(r.unit, []).append(r)
    units = sorted({*by_unit, *user.picks, *(u for u, ls in user.locks.items() if ls.locked)})
    return {u: resolve(u, refits=by_unit.get(u, ()), pick=user.picks.get(u), lock=user.locks.get(u),
                       has_fast=u in fast_units, inputs_now=inputs_now) for u in units}


def fit_label(line: RefitLine | None, batches) -> str:
    """"Fast solo" | "Batch 2 · ring 1" | "Batch 3 · sweep 4"; ``batches`` maps batch id to ``BatchInfo``."""
    if line is None:
        return "Fast solo"
    info = batches.get(line.batch)
    number = info.number if info is not None else line.batch
    return f"Batch {number} · {line.stage} {line.index}"


def overlay_outcome(oc: dict, overlay: dict) -> dict:
    """``oc`` with the overlay's drawable keys in place of the solo fit's, None where the overlay lacks one, and the
    solo verdict keys blanked: no solo number may sit beside a refit's."""
    out = {**oc}
    for k in SOLO_VERDICT_KEYS:
        out[k] = None
    for k in OVERLAY_KEYS:
        out[k] = overlay.get(k)
    for k in CONTEXT_KEYS:
        if k in overlay:
            out[k] = overlay[k]
    return out


def effective(root, outcomes: Mapping[str, dict], tabulated, rows=None, index=None) -> dict[str, Effective]:
    """One entry per spectrum with any refit (``outcomes``: unit -> its stored solo outcome); a unit absent from
    the result is its solo fit. A refit in use whose stored overlay is gone falls back to the solo outcome, noted."""
    root = Path(root)
    log_, user = records.read_log(root), read_user(root)
    units = {r.unit for r in log_.refits}
    if not units:
        return {}
    needed = sorted({*units, *(c.unit for r in log_.refits for c in r.conditioned_on)})
    if rows is None or index is None:
        from isar.intake import inputs, scans as intake_scans, store
        read = store.read_all(root, store.ENTRIES)
        rows = intake_scans.read_scans(root) if rows is None else rows
        index = inputs.index_entries(read.entries, lines=read.lines) if index is None else index
    from isar.intake.gpi import entries_in_force      # the gpi entries a refit's inputs hash was made with
    now = records.inputs_now_from(rows, index, tabulated, needed, gpi=entries_in_force(root), data_dir=root / "data")
    res = resolve_all(log_, user, now, set(outcomes))
    lines = {r.fit: r for r in log_.refits}
    out: dict[str, Effective] = {}
    for unit in sorted(units):
        r = res[unit]
        if r.fit is None:
            continue
        line = lines.get(r.fit) if r.fit != FAST else None
        oc = outcomes.get(unit)
        if line is not None:
            payload = records.read_payload(root, line)
            overlay = None if payload is None else payload.get("overlay")
            if overlay is None or oc is None:
                _log.warning("%s: refit %s cannot be drawn (no overlay or no solo outcome); using the solo fit",
                             unit, line.fit)
                r = replace(r, fit=FAST if oc is not None else None, source="fallback",
                            notes=(*r.notes, "the stored refit cannot be drawn; the solo fit stands in"))
                line = None
                if oc is None:
                    continue
            else:
                oc = overlay_outcome(oc, overlay)
        elif oc is None:
            continue
        out[unit] = Effective(unit, oc, r, fit_label(line, log_.batches), line)
    return out
