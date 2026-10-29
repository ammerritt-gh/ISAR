"""Persistent mode identity across neighbours (C-identity). Stdlib only; never prices (S1U2 does).

S1U1 writes the boundary types and stub signatures from the S1U3 blueprint; S1U3 fills the bodies.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from isar.analyze.coupled.settings import InterSettings

ModeRef = tuple[str, str, int]     # spectrum key, immutable fit_id, sorted index in THAT fit


@dataclass(frozen=True)
class ModeObs:
    ref: ModeRef
    omega0: float; gamma: float; height: float
    sigma_guard: float | None; sigma_provenance: dict
    branch: str | None              # persistent id, never the current sorted index


@dataclass(frozen=True)
class Match:
    branch: str                    # held branch identity (deduplicated across neighbours)
    local: ModeRef | None           # None = not observed locally; no implied death
    term_ids: tuple[str, ...]


@dataclass
class IdentityResult:
    ids: dict[ModeRef, str | None]   # selected display id; None when no justified selection
    status: dict[ModeRef, str]       # matched | new_branch | ambiguous | unresolved_return
    alternatives: dict               # whole association hypotheses, core weights and their scope
    absent: dict[str, list[dict]]    # key -> {branch, last_seen_fit, gap/status} records
    loops: list                      # conflict witnesses: links, modes, competing identities
    drift: dict                      # (analyzer, branch, observed segment) -> endpoint diagnostics
    seams: list                      # same-ring diagnostic conflicts, no fitted factor added
    flags: dict                      # stable reason code -> severity, affected IDs, values


def match_link(local: Sequence[ModeObs], branches: Sequence[str], proposal_costs, incoming=None,
               keep: int = 4) -> dict:
    raise NotImplementedError("stub: filled by S1U3")


def propagate_ids(order, links, modes, settings: InterSettings, *, batch_mode, rings, snapshot_id,
                  assignments, previous) -> IdentityResult:
    raise NotImplementedError("stub: filled by S1U3")


def new_branch_id(fit: str, index: int) -> str:
    raise NotImplementedError("stub: filled by S1U3")


def drift_report(segments, settings: InterSettings) -> dict:
    raise NotImplementedError("stub: filled by S1U3")
