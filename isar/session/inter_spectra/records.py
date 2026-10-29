"""The machine store of inter-spectra fits: the index, the payload files and their readers (C-records).

Types and stubs in S1U1; S1U5 fills the readers and the ids. Stdlib plus the intake package only.
The index ``report/inter_spectra.jsonl`` is append-only, one JSON object per line; payloads live in
``report/inter_spectra/refits/<batch>.jsonl`` and are read by byte offset.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

LOG = "report/inter_spectra.jsonl"
PAYLOAD_DIR = "report/inter_spectra/refits"
SCHEMA_LOG = "isar.inter-log/1"
SCHEMA_REFIT = "isar.inter-refit/1"
SCHEMA_SAVE = "isar.inter-result/1"
ELIGIBLE = ("accepted", "unchanged")        # statuses that are offered fits; "rejected" and "failed" are logged only


def new_batch_id() -> str:                  # "b" + 12 hex; the GUI controller mints its own
    raise NotImplementedError("stub: filled by S1U5")


def new_fit_id() -> str:                    # "f" + 12 hex ("fast" is the reserved id of the solo fit)
    raise NotImplementedError("stub: filled by S1U5")


@dataclass(frozen=True)
class Cond:
    unit: str; fit: str; inputs: str | None          # a neighbour fit conditioned on, and the inputs hash that fit was made from


@dataclass(frozen=True)
class Accept:
    decision: str; reason: str | None; detail: str | None; intake_seq: int
    # decision "authoritative" | "declined"; reason None | "locked" | "inputs_changed" | "neighbour_inputs_changed" | "neighbour_fit_changed"
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


def read_log(root) -> InterLog:                 # tolerant; a repeated fit id keeps its first line
    raise NotImplementedError("stub: filled by S1U5")


def read_payload(root, line: RefitLine) -> dict | None:     # {"came_in","came_out","overlay"}; None (logged) if gone
    raise NotImplementedError("stub: filled by S1U5")


def inputs_now_from(rows, index, tabulated, units: Sequence[str]) -> dict[str, str | None]:
    raise NotImplementedError("stub: filled by S1U5")
