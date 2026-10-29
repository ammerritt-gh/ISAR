"""The batch preview: the line shown before Start and the reason Start is disabled (C-graph). Pure.

S1U4 fills the bodies.
"""
from __future__ import annotations

from dataclasses import dataclass

from isar.session.inter_spectra.graph import Direction, LinkGraph
from isar.session.inter_spectra.rings import AnchoredPlan
from isar.session.inter_spectra.schedule import SimultaneousPlan


@dataclass(frozen=True)
class Preview:
    text: str                                        # "2 anchors · spread in q · 56 spectra · 6 rings"
    can_start: bool
    blocked: str | None                              # the Start tooltip when not can_start
    notes: tuple[str, ...]
    n_spectra: int; n_refit: int; n_rings: int; n_not_reached: int; n_terms: int; n_groups: int


def preview_anchored(g: LinkGraph, plan: AnchoredPlan, direction: Direction) -> Preview:
    raise NotImplementedError("stub: filled by S1U4")


def preview_simultaneous(g: LinkGraph, plan: SimultaneousPlan) -> Preview:
    raise NotImplementedError("stub: filled by S1U4")
