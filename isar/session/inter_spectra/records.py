"""The machine store of inter-spectra fits: the index, the payload files and their readers (C-records).

Stdlib plus the intake package only. The index ``report/inter_spectra.jsonl`` is append-only, one JSON object per
line, file order = global order; payloads live in ``report/inter_spectra/refits/<batch>.jsonl`` and are read by
byte offset. The one writer is ``isar.session.writer.save_inter``; this module only reads.
"""
from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from isar.intake import store

log = logging.getLogger(__name__)

LOG = "report/inter_spectra.jsonl"
PAYLOAD_DIR = "report/inter_spectra/refits"
SCHEMA_LOG = "isar.inter-log/1"
SCHEMA_REFIT = "isar.inter-refit/1"
SCHEMA_SAVE = "isar.inter-result/1"
ELIGIBLE = ("accepted", "unchanged")        # statuses that are offered fits; "rejected" and "failed" are logged only
STATUSES = ("accepted", "unchanged", "rejected", "failed")
# the plain words of a declined refit, by Accept.reason; "{}" is Accept.detail (the neighbour)
REFUSED = {"locked": "locked since the batch started",
           "picked": "you picked a fit for it since the batch started",
           "inputs_changed": "its inputs changed during the batch",
           "neighbour_inputs_changed": "the inputs of {} changed during the batch",
           "neighbour_fit_changed": "the fit of {} it was conditioned on is no longer the one in use"}


def new_batch_id() -> str:                  # "b" + 12 hex; the GUI controller mints its own
    return "b" + secrets.token_hex(6)


def new_fit_id() -> str:                    # "f" + 12 hex ("fast" is the reserved id of the solo fit)
    return "f" + secrets.token_hex(6)


@dataclass(frozen=True)
class Cond:
    unit: str; fit: str; inputs: str | None          # a neighbour fit conditioned on, and the inputs hash that fit was made from


@dataclass(frozen=True)
class Accept:
    decision: str; reason: str | None; detail: str | None; intake_seq: int
    # decision "authoritative" | "declined"; reason None | "locked" | "picked" | "inputs_changed" | "neighbour_inputs_changed" | "neighbour_fit_changed"
    # detail: the locked fit id, or the neighbour unit; intake_seq: highest seq of intake/inter_spectra.jsonl the check saw


@dataclass(frozen=True)
class Flag:
    score: float | None; flagged: bool; reasons: tuple[dict, ...]     # {"code","severity":"info"|"warn"|"serious","text"}


@dataclass(frozen=True)
class RefitLine:
    batch: str; fit: str; unit: str; at: str
    stage: str; index: int                    # "ring" | "sweep", 1-based
    status: str; reason: str | None           # core verdict: accepted | unchanged | rejected | failed
    seed: str | None                          # the fit this refit started from ("fast" or an f-id)
    inputs: str | None                        # own inputs hash (N1); None outside BL43
    conditioned_on: tuple[Cond, ...]
    flag: Flag | None; why_changed: str | None
    accept: Accept | None                     # None for rejected/failed
    payload: tuple[str, int, int] | None      # (file relative to report/inter_spectra/refits, byte offset, byte length)


@dataclass(frozen=True)
class StageLine:
    batch: str; at: str; stage: str; index: int; round: int | None; progress: dict; seams: tuple; not_reached: dict


@dataclass(frozen=True)
class BatchInfo:
    batch: str; number: int; at: str          # number: 1-based ordinal, assigned by the writer ("Batch 2")
    batch_kind: str                           # "anchored" | "simultaneous"
    scope: dict; anchors: tuple[str, ...]; rings: dict; seams: tuple; settings: dict; preview: str   # as given at start, never edited
    fixed: dict; unreached: dict; terms: tuple; metric: dict; core: str; intake_seq: int; epochs: dict   # as given at start
    end: dict | None                          # batch_end line; None = open


@dataclass(frozen=True)
class InterLog:
    batches: dict[str, BatchInfo]; refits: tuple[RefitLine, ...]; stages: tuple[StageLine, ...]; damaged: tuple


def refused_words(accept: Accept) -> str:
    """The plain reason a declined refit gives in ``save_inter``'s verdict."""
    return REFUSED[accept.reason].format(accept.detail)


def _batch(o: dict) -> BatchInfo:
    return BatchInfo(o["batch"], int(o["number"]), o["at"], o["batch_kind"], dict(o["scope"]), tuple(o["anchors"]),
                     dict(o["rings"]), tuple(o["seams"]), dict(o["settings"]), o["preview"], dict(o["fixed"]),
                     dict(o["unreached"]), tuple(o["terms"]), dict(o["metric"]), o["core"], int(o["intake_seq"]),
                     dict(o["epochs"]), None)


def _refit(o: dict) -> RefitLine:
    f, a, p = o["flag"], o["accept"], o["payload"]
    return RefitLine(
        o["batch"], o["fit"], o["unit"], o["at"], o["stage"], int(o["index"]), o["status"], o["reason"], o["seed"],
        o["inputs"], tuple(Cond(c["unit"], c["fit"], c["inputs"]) for c in o["conditioned_on"]),
        None if f is None else Flag(f["score"], bool(f["flagged"]), tuple(f["reasons"])), o["why_changed"],
        None if a is None else Accept(a["decision"], a["reason"], a["detail"], int(a["intake_seq"])),
        None if p is None else (str(p[0]), int(p[1]), int(p[2])))


def _stage(o: dict) -> StageLine:
    return StageLine(o["batch"], o["at"], o["stage"], int(o["index"]), o["round"], dict(o["progress"]),
                     tuple(o["seams"]), dict(o["not_reached"]))


_END_SKIP = ("schema", "kind", "batch")


def read_log(root) -> InterLog:
    """The index, tolerant: a line that does not parse or does not fit its kind is skipped (logged) and listed in
    ``damaged``; a repeated fit id keeps its first line; a line of a batch with no ``batch_start`` before it is
    skipped and listed; a second ``batch_end`` is ignored."""
    damaged: list = []
    batches: dict[str, BatchInfo] = {}
    refits: dict[str, RefitLine] = {}
    stages: list[StageLine] = []
    for lineno, o in store._objects(store._bytes(Path(root) / LOG), LOG, damaged):
        kind = o.get("kind")
        try:
            if o.get("schema") != SCHEMA_LOG or kind not in ("batch_start", "refit", "stage", "batch_end"):
                raise ValueError(f"not an {SCHEMA_LOG} line")
            if kind == "batch_start":
                if o["batch"] not in batches:
                    batches[o["batch"]] = _batch(o)
                continue
            if o.get("batch") not in batches:
                raise ValueError(f"{kind} of batch {o.get('batch')!r}, which has no batch_start before it")
            if kind == "refit":
                line = _refit(o)
                if line.fit in refits:
                    log.info("%s line %d: fit %s already logged; the first line stands", LOG, lineno, line.fit)
                else:
                    refits[line.fit] = line
            elif kind == "stage":
                stages.append(_stage(o))
            elif batches[o["batch"]].end is None:
                batches[o["batch"]] = replace(batches[o["batch"]],
                                              end={k: v for k, v in o.items() if k not in _END_SKIP})
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            store._skip(damaged, LOG, lineno, f"{type(exc).__name__}: {exc}")
    return InterLog(batches, tuple(refits.values()), tuple(stages),
                    tuple(sorted(damaged, key=lambda d: d.line)))


def read_payload(root, line: RefitLine) -> dict | None:
    """``{"came_in", "came_out", "overlay"}`` of a logged refit; None (logged) if it has no payload or it is gone."""
    if line.payload is None:
        return None
    name, offset, length = line.payload
    path = Path(root) / PAYLOAD_DIR / name
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            o = json.loads(f.read(length))
        if not isinstance(o, dict) or o.get("fit") != line.fit:
            raise ValueError(f"the bytes at {offset} are not fit {line.fit}'s payload")
    except (OSError, ValueError) as exc:
        log.warning("payload of %s (%s) unreadable: %s", line.fit, path, exc)
        return None
    return {"came_in": o.get("came_in"), "came_out": o.get("came_out"), "overlay": o.get("overlay")}


def inputs_now_from(rows, index, tabulated, units: Sequence[str], gpi: Mapping | None = None) -> dict[str, str | None]:
    """Each unit's inputs hash now (N1: the hash ``file_fit`` stamps), from the import rows, the intake entry index,
    the stored tabulated calibration and the gpi entries in force (``gpi.entries_in_force``, by scan key; None: none);
    None for a unit with no import row (outside BL43: not judged)."""
    from isar.intake import inputs     # here: it loads numpy, and the pure authority module imports this one
    out: dict[str, str | None] = {}
    scan_inputs: dict = {}
    for unit in units:
        scan, _, det = unit.rpartition(":")
        row = rows.get(scan)
        if row is None:
            out[unit] = None
            continue
        if scan not in scan_inputs:
            scan_inputs[scan] = inputs.scan_inputs(row, index, (gpi or {}).get(scan))
        out[unit] = inputs.inputs_hash(inputs.unit_inputs(row, scan_inputs[scan], det, tabulated.get(scan)))
    return out
