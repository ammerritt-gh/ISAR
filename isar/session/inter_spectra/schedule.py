"""The simultaneous plan: free and held spectra, terms, conflict-free parallel groups (C-graph).

Pure, stdlib only. S1U4 fills the bodies.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from isar.session.inter_spectra.graph import LinkGraph, Term


@dataclass(frozen=True)
class SimultaneousPlan:
    free: tuple[str, ...]                            # refitted; selection, eligible, unlocked, in >= 1 term
    held: tuple[str, ...]                            # locked members of the terms (fixed neighbours)
    terms: tuple[Term, ...]                          # T-change per T-link + curvature triplets; each has >= 1 free member
    terms_of: dict[str, tuple[int, ...]]             # free key -> indices into terms (the refit includes ALL of them)
    groups: tuple[tuple[str, ...], ...]              # conflict-free parallel groups, run in this order within a sweep
    not_coupled: dict[str, str]                      # selected keys that take no part -> reason


def plan_simultaneous(g: LinkGraph, locks: Collection[str]) -> SimultaneousPlan:
    raise NotImplementedError("stub: filled by S1U4")


def check_groups(plan: SimultaneousPlan) -> None:          # raises AssertionError naming the first violation
    raise NotImplementedError("stub: filled by S1U4")
