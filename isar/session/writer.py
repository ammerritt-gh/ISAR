"""The writers that run beside the analysis queue: ``save_results`` (C-save), ``save_inter`` (the inter-spectra
store) and ``isar ingest``'s merge.

All three hold the workspace write lock (:func:`isar.session.atomic.workspace_write_lock`) for their whole
read-modify-write and merge onto the files as they are on disk, never over them with a stale snapshot:

- :func:`save_results` re-reads the ledger and card state, replaces only each payload's own analyzer pass
  (a fast pass replaces the entry, a deep pass sets only its record), and refuses a payload whose data
  was re-read, or whose fast pass was replaced, while it was being fitted.
- :func:`save_session_merged` saves an ingest's ledger by a three-way merge against the snapshot taken
  when it loaded, so a result saved meanwhile is kept.
- :func:`save_inter` appends a batch's refits to ``report/inter_spectra`` and decides, against the locks and
  input fingerprints on disk at that moment, which become authoritative; it writes nothing else.

``run_batch``, the campaign worker and the drive loop do not take the lock (docs/WORKSPACE_AND_CLI.md).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path

from isar.intake import fit_inputs, inputs, inter_spectra, scans as intake_scans, store
from isar.schema import _json
from isar.schema.card import load_state
from isar.session import calibrations, measurement_store
from isar.session.atomic import workspace_write_lock
from isar.session.inter_spectra import records
from isar.session.outcome import _write_results_csv
from isar.session.state import SessionState, _scan_entry_from_dict, canonical_hash
from isar.session.workspace import STATE_NAME, Workspace

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
    propagate. ``isar.session.inter_spectra.records`` holds the file contract. It never touches session.json,
    results.csv or a fast solo fit, and never writes a pick, lock or anchor (it only reads them).
    """
    if isinstance(payloads, (str, bytes, dict)) or not isinstance(payloads, Sequence):
        raise PayloadError(f"save_inter takes a sequence of payloads, not {type(payloads).__name__}")
    payloads = list(payloads)
    problems = [_inter_problem(p) for p in payloads]
    root = Path(workspace_root)
    with workspace_write_lock(root):
        logged = records.read_log(root)
        user = inter_spectra.read_user(root)
        starts = {b: info.intake_seq for b, info in logged.batches.items()}
        ended = {b for b, info in logged.batches.items() if info.end is not None}
        number = max((info.number for info in logged.batches.values()), default=0)
        accepts = {r.fit: r.accept for r in logged.refits}
        staged = {(s.batch, s.stage, s.index, s.round) for s in logged.stages}
        declined: set[str] = set()                 # fit ids declined in this call
        now: dict[str, str | None] = {}
        reads = []                                 # (rows, index, tabulated), read once, inside the lock
        out: list[dict | str] = []
        for p, why in zip(payloads, problems):
            if why is not None:
                out.append(why)
                continue
            batch, event = p["batch"], p["event"]
            verdict = {"saved": True, "accepted": {}, "refused": {}}
            head = {"schema": records.SCHEMA_LOG, "batch": batch, "at": fit_inputs._now()}
            if event == "start":
                if batch not in starts:            # a repeated start is a retry: nothing written again
                    number += 1
                    _append_lines(root / records.LOG, [_line({**head, "kind": "batch_start", "number": number,
                                                              **p["start"]})])
                    starts[batch] = p["start"]["intake_seq"]
                out.append(verdict)
                continue
            if batch not in starts:
                out.append(f"unknown batch {batch}")
                continue
            if event == "end":
                if batch not in ended:             # the first end of a batch wins
                    _append_lines(root / records.LOG, [_line({**head, "kind": "batch_end", **p["end"]})])
                    ended.add(batch)
                out.append(verdict)
                continue
            body = p["stage"]
            fresh = [r for r in {r["fit"]: r for r in body["refits"]}.values() if r["fit"] not in accepts]
            needed = [u for r in fresh if r["status"] in records.ELIGIBLE
                      for u in (r["unit"], *(c["unit"] for c in r["conditioned_on"])) if u not in now]
            if needed:
                if not reads:
                    entries = store.read_all(root, store.ENTRIES)
                    reads.append((intake_scans.read_scans(root),
                                  inputs.index_entries(entries.entries, lines=entries.lines),
                                  load_state(root / STATE_NAME).tabulated_calibration))
                now.update(records.inputs_now_from(*reads[0], needed))
            blobs, lines = [], []
            for r in fresh:
                accept = None
                if r["status"] in records.ELIGIBLE:
                    accept = _decide(r, user, now, starts[batch], declined)
                    if accept.decision == "declined":
                        declined.add(r["fit"])
                accepts[r["fit"]] = accept
                pl = r.get("payload")
                if pl is not None:
                    blobs.append(_line({"schema": records.SCHEMA_REFIT, "fit": r["fit"], "unit": r["unit"],
                                        "came_in": pl.get("came_in"), "came_out": pl.get("came_out"),
                                        "overlay": pl.get("overlay")}))
                lines.append((r, accept, pl is not None))
            name = f"{batch}.jsonl"
            offset = _append_lines(root / records.PAYLOAD_DIR / name, blobs)
            index = []
            sizes = iter(len(b) for b in blobs)
            for r, accept, has_payload in lines:
                pointer = None
                if has_payload:
                    size = next(sizes)
                    pointer, offset = [name, offset, size - 1], offset + size     # the length leaves out the newline
                index.append(_line({
                    **head, "kind": "refit", "fit": r["fit"], "unit": r["unit"], "stage": body["stage"],
                    "index": body["index"], "status": r["status"], "reason": r.get("reason"), "seed": r.get("seed"),
                    "inputs": r.get("inputs"),
                    "conditioned_on": [{"unit": c["unit"], "fit": c["fit"], "inputs": c.get("inputs")}
                                       for c in r["conditioned_on"]],
                    "flag": r.get("flag"), "why_changed": r.get("why_changed"),
                    "accept": None if accept is None else dataclasses.asdict(accept), "payload": pointer}))
            key = (batch, body["stage"], body["index"], body.get("round"))
            if key not in staged:                  # a repeated stage is a retry: its line is written once
                staged.add(key)
                index.append(_line({**head, "kind": "stage", "stage": body["stage"], "index": body["index"],
                                    "round": body.get("round"), "progress": body.get("progress") or {},
                                    "seams": body.get("seams") or [], "not_reached": body.get("not_reached") or {}}))
            _append_lines(root / records.LOG, index)    # index last: a payload no index line names is never read
            for r in body["refits"]:
                accept = accepts.get(r["fit"])
                if accept is None:
                    continue
                if accept.decision == "authoritative":
                    verdict["accepted"][r["unit"]] = r["fit"]
                else:
                    verdict["refused"][r["unit"]] = records.refused_words(accept)
            out.append(verdict)
    return out


_BATCH_ID = re.compile(r"b[0-9a-f]{12}")
_FIT_ID = re.compile(r"f[0-9a-f]{12}")
_START = {"batch_kind": str, "scope": dict, "anchors": list, "rings": dict, "seams": list, "settings": dict,
          "preview": str, "fixed": dict, "unreached": dict, "terms": list, "metric": dict, "core": str,
          "intake_seq": int, "epochs": dict}


def _inter_problem(p) -> str | None:
    """Why an inter payload cannot be saved (nothing of it is written); None when it can."""
    if not isinstance(p, dict) or p.get("schema") != records.SCHEMA_SAVE:
        return f"not an {records.SCHEMA_SAVE} payload"
    batch, event = p.get("batch"), p.get("event")
    if not (isinstance(batch, str) and _BATCH_ID.fullmatch(batch)) or p.get("unit") != f"inter:{batch}:batch" \
            or p.get("pass") != "inter":
        return f"unit {p.get('unit')!r} is not 'inter:<batch>:batch' of a batch id b + 12 hex ({batch!r})"
    if event not in ("start", "stage", "end") or not isinstance(p.get(event), dict):
        return f"event {event!r} is not start, stage or end with its block"
    body = p[event]
    if event == "start":
        bad = [k for k, t in _START.items() if not isinstance(body.get(k), t) or isinstance(body.get(k), bool)]
        return f"a start lacks {', '.join(bad)}" if bad else None
    if event == "end":
        return None if isinstance(body.get("state"), str) else "an end carries no state"
    if body.get("stage") not in ("ring", "sweep") or not isinstance(body.get("index"), int) \
            or not isinstance(body.get("refits"), list):
        return "a stage carries no stage (ring or sweep), index or refits"
    for r in body["refits"]:
        if not isinstance(r, dict) or not isinstance(r.get("fit"), str) or not _FIT_ID.fullmatch(r["fit"]):
            return f"a refit's fit id {r.get('fit') if isinstance(r, dict) else r!r} is not f + 12 hex"
        fit, unit, status, pl = r["fit"], r.get("unit"), r.get("status"), r.get("payload")
        if not isinstance(unit, str) or unit.count(":") < 2:
            return f"refit {fit}: unit {unit!r} is not a spectrum key"
        if status not in records.STATUSES:
            return f"refit {fit}: status {status!r} is not one of {records.STATUSES}"
        conds = r.get("conditioned_on")
        if not isinstance(conds, list) or not all(isinstance(c, dict) and isinstance(c.get("unit"), str)
                                                  and isinstance(c.get("fit"), str) for c in conds):
            return f"refit {fit}: conditioned_on is not a list of {{unit, fit, inputs}}"
        flag = r.get("flag")
        if flag is not None and not (isinstance(flag, dict) and isinstance(flag.get("reasons"), list)
                                     and "flagged" in flag and "score" in flag):
            return f"refit {fit}: flag is not {{score, flagged, reasons}}"
        if pl is not None and not isinstance(pl, dict):
            return f"refit {fit}: payload is not an object"
        if status in records.ELIGIBLE and not (pl is not None and isinstance(pl.get("overlay"), dict)):
            return f"refit {fit} is {status} but carries no overlay"
    return None


def _decide(r: dict, user, now: dict, start_seq: int, declined: set) -> records.Accept:
    """C-save-inter's acceptance check of one eligible refit, in the contract's order; ``now`` is inputs_now."""
    def no(reason, detail=None):
        return records.Accept("declined", reason, detail, user.max_seq)
    lock = user.locks.get(r["unit"])
    if lock is not None and lock.locked:
        return no("locked", lock.fit)
    own = now.get(r["unit"])
    if own is not None and own != r.get("inputs"):
        return no("inputs_changed")
    conds = r["conditioned_on"]
    for c in conds:
        n = now.get(c["unit"])
        if n is not None and n != c.get("inputs"):
            return no("neighbour_inputs_changed", c["unit"])
    for c in conds:
        pick, lk = user.picks.get(c["unit"]), user.locks.get(c["unit"])
        if (c["fit"] in declined
                or (pick is not None and pick.seq > start_seq and pick.fit != c["fit"])
                or (lk is not None and lk.locked and lk.seq > start_seq and lk.fit != c["fit"])):
            return no("neighbour_fit_changed", c["unit"])
    return records.Accept("authoritative", None, None, user.max_seq)


def _line(obj: dict) -> bytes:
    """One strict-JSON line; a non-finite float is written as null."""
    return json.dumps(inputs._finite(obj), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def _append_lines(path: Path, blobs: Sequence[bytes]) -> int:
    """One open, one write and one fsync (as ``fit_inputs._append``); the byte offset the first blob lands at."""
    if not blobs:
        return 0
    data = b"".join(blobs)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        if f.seek(0, os.SEEK_END):
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                log.warning("%s: ended an unterminated last line (a torn write) with a newline", path)
                data = b"\n" + data
        start = f.tell() + len(data) - sum(len(b) for b in blobs)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return start


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
