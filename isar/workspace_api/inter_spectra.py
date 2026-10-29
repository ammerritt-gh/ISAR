"""The read facade over inter-spectra fitting (C-gui-reads, C-authority, C-preview). Qt-free, read-only.

This module never writes (AGENTS.md hard rules 6 and 7). S1U1 writes the types and stubs; S1U5 fills the reads
on the authority resolver. The GUI and the notebooks read through here.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .views import FitView

Severity = Literal["info", "warn", "serious"]    # info: a repair (neighbour-supported split); warn; serious: tension, unresolved identity, did not settle


@dataclass(frozen=True)
class FlagReason:
    code: str; severity: Severity; detail: str = ""     # detail: backend text with numbers, shown on hover


@dataclass(frozen=True)
class FlagInfo:
    severity: Severity; score: float | None; cost_of_coupling: float | None; link_mismatch: float | None
    reasons: tuple[FlagReason, ...]


@dataclass(frozen=True)
class SpectrumInter:
    key: str; fit_id: str                 # the authoritative fit ("fast" or a refit id), stale rule already applied
    in_use: bool                          # fit_id != "fast"
    source: Literal["fast", "batch", "pick", "lock"]
    label: str                            # "Fast solo" | "Batch 2 · sweep 3"
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
    changed: bool                                          # False: an unchanged refit (collapsed in the menu)
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


def inter_overview(root) -> InterOverview:                 # empty overview (revision "0") when nothing exists
    raise NotImplementedError("stub: filled by S1U5")


def inter_history(root, key: str) -> tuple[FitEntry, ...]:  # oldest first; [0] is Fast solo
    raise NotImplementedError("stub: filled by S1U5")


def batch_preview(root, req: BatchRequest) -> BatchPreview:
    raise NotImplementedError("stub: filled by S1U5")


def fit_view_of(ws, unit: str, fit: str = "authoritative") -> FitView | None:
    raise NotImplementedError("stub: filled by S1U5")
