"""The inter-spectra batch runner (C-queue). Qt-free; top-level imports stdlib only, like analysis_service.

S1U1 writes the boundary dataclasses and the stub; S1U6 fills ``run_batch``. The runner sees only the
``emit``, ``progress`` and ``ack`` callables; the service owns the pipes.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal


@dataclass(frozen=True)
class BatchSpec:                       # JSON-able; the "batch" object of the start command
    batch_id: str                      # "b" + 12 hex, minted by the GUI controller; unique per experiment
    kind: Literal["anchored", "simultaneous"]
    direction: Literal["q", "T", "qT"] | None   # anchored: the user's choice; simultaneous: None
    anchors: tuple[str, ...]           # spectrum keys; () for simultaneous
    members: tuple[str, ...]           # = BatchPreview.members: the scope, anchors included

    def to_json(self) -> dict:
        raise NotImplementedError("stub: filled by S1U6")

    @staticmethod
    def from_json(d: dict) -> "BatchSpec":                 # raises ValueError naming the bad field
        raise NotImplementedError("stub: filled by S1U6")


@dataclass(frozen=True)
class BatchItem:                       # what the item process receives (like file_fit.FileItem)
    item_id: str
    unit_key: str                      # "inter:<batch_id>:batch"
    pass_kind: str                     # "inter"
    cores: int                         # >= 1
    spec: dict                         # BatchSpec.to_json()


@dataclass(frozen=True)
class InterResult:                     # dataclasses.asdict() of this goes down the item pipe as ("result", ...)
    item_id: str
    unit_key: str
    pass_kind: str                     # "inter" | "cores"   (same field names as file_fit.FileResult)
    status: str                        # "done" | "done_warning" | "failed" | "cores"
    reason: str | None
    payload: dict | None               # isar.inter-result/1; None for "cores"
    cores: int | None = None
    ack: int | None = None             # token the runner will wait on; None = no wait


def run_batch(workspace_root: Path, item: BatchItem, *, progress: Callable[[dict], None],
              emit: Callable[[InterResult], None], ack: Callable[[int], dict]) -> None:
    # never raises for an analysis failure: it is logged and emitted as event "end" outcome "failed".
    # ack(token) blocks until the service has saved that result and returns the verdict.
    raise NotImplementedError("stub: filled by S1U6")
