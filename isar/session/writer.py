"""The writers that run beside the analysis queue: ``save_results`` (C-save) and ``isar ingest``'s merge.

Both hold the workspace write lock (:func:`isar.session.atomic.workspace_write_lock`) for their whole
read-modify-write and merge onto the files as they are on disk, never over them with a stale snapshot:

- :func:`save_results` re-reads the ledger and card state, replaces only each payload's own analyzer pass
  (a fast pass replaces the entry, a deep pass sets only its record), and refuses a payload whose data
  was re-read, or whose fast pass was replaced, while it was being fitted.
- :func:`save_session_merged` saves an ingest's ledger by a three-way merge against the snapshot taken
  when it loaded, so a result saved meanwhile is kept.

``run_batch``, the campaign worker and the drive loop do not take the lock (docs/WORKSPACE_AND_CLI.md).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

from isar.intake import fit_inputs, scans as intake_scans
from isar.schema import _json
from isar.session import calibrations, measurement_store
from isar.session.atomic import workspace_write_lock
from isar.session.outcome import _write_results_csv
from isar.session.state import SessionState, _scan_entry_from_dict, canonical_hash
from isar.session.workspace import Workspace

log = logging.getLogger(__name__)

SCHEMA = "isar.file-result/1"
_PASSES = ("fast", "deep", "attempt")
_NOT_HISTORY = ("ingested", "excluded")     # a replaced entry in these states holds no fit to keep
FAST_STALE = "the file was read in again with different data while it was being fitted; add it again"
DEEP_STALE = "its fast pass was replaced while the deep pass ran; add it again"


class PayloadError(ValueError):
    """A payload is malformed; nothing of the call was written."""


class ProjectionStale(OSError):
    """session.json is saved (steps 1-7 done) but results.csv/modes.csv could not be rebuilt.
    ``.verdicts`` holds the per-payload list save_results would have returned; ``.cause`` the OSError."""

    def __init__(self, verdicts: list, cause: OSError):
        super().__init__(f"saved; results.csv could not be rebuilt: {cause}")
        self.verdicts, self.cause = verdicts, cause


def _validate(i: int, p) -> None:
    def bad(why):
        raise PayloadError(f"payload {i}: {why}")
    if not isinstance(p, dict) or p.get("schema") != SCHEMA:
        bad(f"not an {SCHEMA} payload")
    unit, kind = p.get("unit"), p.get("pass")
    if not isinstance(unit, str) or unit.count(":") < 2:
        bad(f"unit {unit!r} is not 'experiment:scan:detector'")
    if kind not in _PASSES:
        bad(f"pass {kind!r} is not one of {_PASSES}")
    if kind == "attempt":
        if not isinstance(p.get("measurement_record"), dict):
            bad("an attempt carries no measurement_record")
        return
    if not isinstance(p.get("base"), dict):
        bad("no base")
    if kind == "fast" and not (isinstance(p.get("entry"), dict) and p["entry"].get("scan_id") == unit):
        bad(f"a fast payload's entry is not {unit}'s")
    if kind == "deep" and not (isinstance(p.get("deep"), dict) and isinstance(p["deep"].get("record"), dict)):
        bad("a deep payload carries no deep record")
    if not isinstance(p.get("inputs_at"), int):
        bad("no inputs_at (when its inputs were read)")
    fi = p.get("fit_inputs")
    if fi is not None and not (isinstance(fi, dict) and isinstance(fi.get("hash"), str)
                               and all(isinstance(fi.get(k), list) for k in ("fallbacks", "started", "finished"))):
        bad("fit_inputs is not {hash, fallbacks, started, finished}")


def _apply(session: SessionState, p: dict) -> str | None:
    """Apply one fast or deep payload to the in-memory ledger; the stale reason when refused."""
    unit, base = p["unit"], p["base"]
    cur = session.get(unit)
    if p["pass"] == "fast":
        if cur is not None and cur.content_hash != base.get("content_hash"):
            return FAST_STALE
        new = _scan_entry_from_dict(p["entry"])
        if cur is not None:
            new.provenance = {**(cur.provenance or {}), **(new.provenance or {})}
        session.put(new)
        return None
    det = unit.rsplit(":", 1)[1]
    oc = None if cur is None else (cur.detectors or {}).get(det)
    if (oc is None or cur.content_hash != base.get("content_hash")
            or canonical_hash(oc.get("fast_pass")) != base.get("fast_id")):
        return DEEP_STALE
    oc["deep_pass"] = p["deep"]["record"]
    cur.provenance = {**(cur.provenance or {}), "deep_pass": p["deep"].get("timing")}
    fi = p.get("fit_inputs")
    if fi is not None:
        cur.provenance["fit_inputs"] = {"run": "", "hash": fi["hash"], "fallbacks": list(fi["fallbacks"])}
    return None


def _apply_calibration(card, p: dict) -> bool:
    """Store the payload's recomputed scan state and append its resolution version, each only when the
    payload's inputs are at least as new as the stored one's (``inputs_at``, stamped on what it stores); a
    stored state without a stamp (``isar run``'s) yields only to a payload that saw it at its start, and a
    version without one to any. Whether card.state changed."""
    at = p["inputs_at"]
    changed = False
    cal = p.get("calibration")
    if cal is not None:
        fresh = card.state.tabulated_calibration.get(cal["scan"])
        if fresh is None or (at >= fresh["inputs_at"] if "inputs_at" in fresh
                             else fresh.get("inputs_key") == cal.get("replaces")):
            card.state.tabulated_calibration[cal["scan"]] = {**cal["state"], "inputs_at": at}
            changed = True
        else:     # a state from newer inputs is stored; the result records its own fingerprint
            log.info("calibration of %s not stored: the stored state is newer than %s's inputs", cal["scan"],
                     p["unit"])
    versions = card.state.resolution_versions
    if p.get("resolution_version") is not None:
        if versions and at < versions[-1].get("inputs_at", at):
            log.info("resolution version of %s not appended: the latest is from newer inputs", p["unit"])
        elif calibrations._append_version_if_changed(card, dict(p["resolution_version"])):
            versions[-1]["inputs_at"] = at
            changed = True
    return changed


def save_results(workspace_root: Path | str, payloads: Sequence[dict]) -> list[str | None]:
    """Under C-lock: re-read the ledger and state, apply each payload in order, write once, rebuild the
    projections. Returns, per payload in order, None (saved) or the plain reason it was not saved
    (stale). Returns only when every write has completed; raises (OSError, WorkspaceBusy, PayloadError)
    on failure, after which no payload of the call may be assumed saved, except ProjectionStale."""
    payloads = list(payloads)
    for i, p in enumerate(payloads):
        _validate(i, p)
    ws = Workspace.open(workspace_root)
    with workspace_write_lock(ws.root):
        card, session = ws.load()
        intake = intake_scans.applies(card)
        verdicts: list[str | None] = []
        history: list[dict] = []     # the superseded line of each changed unit's entry as loaded (BL43)
        changed: set[str] = set()
        saved: list[dict] = []
        state_changed = False
        for p in payloads:
            if p["pass"] == "attempt":
                verdicts.append(None)
                continue
            before = session.get(p["unit"])
            line = None
            if intake and p["unit"] not in changed and before is not None and before.status not in _NOT_HISTORY:
                line = fit_inputs.superseded_line(before, by="refit", run=None)   # a copy, before any change
            why = _apply(session, p)
            verdicts.append(why)
            if why is not None:
                continue
            if line is not None:
                history.append(line)
            changed.add(p["unit"])
            saved.append(p)
            state_changed |= _apply_calibration(card, p)
        saved_ids = {id(p) for p in saved}

        bl43 = [p for p in saved if p.get("fit_inputs") is not None]
        run = None
        if bl43:
            run = fit_inputs.begin_run(ws.root, passes=[k for k in fit_inputs.PASSES
                                                       if any(p["pass"] == k for p in bl43)])
        outcome = "failed"
        try:
            # Every fit a save replaces is kept in full first (C-fit-history), so a crash leaves a repeat.
            fit_inputs.write_superseded(ws.root, [{**ln, "run": run} for ln in history])
            if run is not None:
                fit_inputs.write_started(ws.root, run, [ln for p in bl43 for ln in p["fit_inputs"]["started"]])
                fit_inputs.write_finished(ws.root, run, [ln for p in bl43 for ln in p["fit_inputs"]["finished"]])
                for p in bl43:
                    entry = session.get(p["unit"])
                    stamp = (entry.provenance or {}).get("fit_inputs")
                    if not isinstance(stamp, dict):
                        stamp = {"hash": p["fit_inputs"]["hash"], "fallbacks": list(p["fit_inputs"]["fallbacks"])}
                    entry.provenance = {**(entry.provenance or {}), "fit_inputs": {**stamp, "run": run}}
            for p in payloads:
                record = (p.get("measurement_record") if p["pass"] == "attempt"
                          else (p.get("entry") or {}).get("measurement_record") if id(p) in saved_ids else None)
                if record is not None:
                    measurement_store.append_measurement(ws.report_dir, record)
            if state_changed:
                card.save_state(ws.state_path)
            if saved:
                session.save(ws.session_path)
                outcome = "complete"
                try:
                    _write_results_csv(ws, session)
                except OSError as exc:
                    log.warning("save_results: %s saved; results.csv not rebuilt: %s", ws.session_path, exc)
                    raise ProjectionStale(verdicts, exc) from exc
        finally:
            if run is not None:
                try:
                    fit_inputs.end_run(ws.root, run, outcome)
                except OSError:      # the saves stand; a later run start supersedes this one anyway
                    log.exception("save_results: the end line of %s not written", run)
    return verdicts


def save_inter(workspace_root, payloads: Sequence[dict]) -> list[dict | str]:
    """Inter-spectra payloads (``isar.inter-result/1``), under the write lock: validate all, then apply in order.

    Per payload: a verdict dict ``{"saved": True, "accepted": {unit: fit_id}, "refused": {unit: reason}}`` or a
    str (nothing of that payload was written). Raises PayloadError for a non-sequence; OSError and WorkspaceBusy
    propagate. Filled by S1U5; ``isar.session.inter_spectra.records`` holds the file contract.
    """
    raise NotImplementedError("stub: filled by S1U5")


# --- the merge for a writer that holds a whole session snapshot (isar ingest) -----------------------
def _dumps(obj) -> str:
    return json.dumps(_json.to_jsonable(obj), sort_keys=True)     # NaN allowed, as the ledger writes it


def snapshot(session: SessionState) -> dict:
    """The session as just loaded, per key, for :func:`save_session_merged`.
    shortcut: whole-snapshot JSON compare at load and save (about 1-2 s on a 60 MB ledger); keep a dirty
    set in the writers if it ever dominates."""
    return {"scans": {sid: _dumps(e) for sid, e in session.scans.items()},
            "files": {path: _dumps(row) for path, row in session.files.items()},
            "data_paths": list(session.data_paths),
            "last_run": _dumps(session.last_run), "last_ingest": _dumps(session.last_ingest)}


def _merge3(what: str, base: dict, ours: dict, theirs: dict) -> tuple[dict, list[str]]:
    """Per key: ours unchanged since base -> theirs; theirs unchanged -> ours; both changed -> ours (logged).
    An absent key is None. Returns the merged mapping (theirs' order, then ours' new keys) and the keys
    both changed differently."""
    merged, both = {}, []
    for key in list(theirs) + [k for k in ours if k not in theirs]:
        b = base.get(key)
        o = _dumps(ours[key]) if key in ours else None
        t = _dumps(theirs[key]) if key in theirs else None
        pick_ours = o != b
        if pick_ours and t != b and t != o:
            both.append(key)
            log.warning("session.json %s %s: changed by another writer and by this one; this writer's value kept",
                        what, key)
        src = ours if pick_ours else theirs
        if key in src:
            merged[key] = src[key]
    return merged, both


def save_session_merged(ws: Workspace, session: SessionState, base: dict) -> dict:
    """Save ``session`` (loaded when ``base`` was taken, then changed) merged onto session.json as it is
    now, under the workspace write lock. ``session`` becomes the merged state; returns its new base.
    A fit saved meanwhile for a unit this writer also changed is kept in ``fit_history.jsonl`` (BL43)."""
    with workspace_write_lock(ws.root):
        card, theirs = ws.load()
        session.scans, both = _merge3("scan", base["scans"], session.scans, theirs.scans)
        if intake_scans.applies(card):
            lines = [fit_inputs.superseded_line(theirs.scans[k], by="reimport", run=None)
                     for k in both if k in theirs.scans and theirs.scans[k].status not in _NOT_HISTORY]
            fit_inputs.write_superseded(ws.root, lines)
        session.files, _ = _merge3("file", base["files"], session.files, theirs.files)
        session.data_paths = theirs.data_paths + [p for p in session.data_paths if p not in theirs.data_paths]
        for name in ("last_run", "last_ingest"):
            merged, _ = _merge3(name, {name: base[name]}, {name: getattr(session, name)},
                                {name: getattr(theirs, name)})
            setattr(session, name, merged[name])
        session.save(ws.session_path)
    return snapshot(session)
