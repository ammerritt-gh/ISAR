"""Rings, seams and the anchored plan (C-graph). Pure, stdlib only; fixed by distance before anything runs.

S1U4 fills the bodies.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from isar.session.inter_spectra.graph import Link, LinkGraph


@dataclass(frozen=True)
class Ring:
    index: int                                       # >= 1
    members: tuple[str, ...]                         # refitted in this ring (locked nodes excluded), sorted
    locked: tuple[str, ...]                          # locked nodes at this distance, not refitted
    conditioners: dict[str, tuple[Link, ...]]        # member -> links to the neighbours it is fitted against


@dataclass(frozen=True)
class Seam:
    ring: int
    link: Link                                       # joins two spectra at the same ring, both >= 1
    fronts: tuple[tuple[str, ...], tuple[str, ...]]  # nearest anchors on each side


@dataclass(frozen=True)
class AnchoredPlan:
    anchors: tuple[str, ...]                         # anchors used (eligible, in scope), sorted
    ignored_anchors: dict[str, str]                  # key -> reason
    ring_of: dict[str, int]                          # reached keys -> ring (anchors 0), locked included
    fronts: dict[str, tuple[str, ...]]               # key -> nearest anchors (ties kept)
    rings: tuple[Ring, ...]                          # 1..max, including a ring with no members
    seams: tuple[Seam, ...]
    not_reached: dict[str, str]                      # key -> reason


def plan_anchored(g: LinkGraph, anchors: Collection[str], locks: Collection[str],
                  stale_locks: Collection[str] = ()) -> AnchoredPlan:
    raise NotImplementedError("stub: filled by S1U4")
