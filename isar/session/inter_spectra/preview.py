"""The batch preview: the line shown before Start and the reason Start is disabled (C-graph). Pure.
"""
from __future__ import annotations

from collections import Counter
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


_DIRECTION_WORDS = {"q": "q", "T": "T", "qT": "q and T"}


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _tally(counts: Counter) -> list[str]:
    return [f"{_n(n, 'spectrum', 'spectra')} left out: {reason}" for reason, n in sorted(counts.items())]


def preview_anchored(g: LinkGraph, plan: AnchoredPlan, direction: Direction) -> Preview:
    n_refit = sum(len(r.members) for r in plan.rings)
    no_q = not any(l.kind == "q" for l in g.links)
    notes = list(g.notes)
    if direction == "qT" and no_q:
        notes.append("q-links unavailable: no accepted hkl; spreading in T only")
    notes += _tally(Counter(plan.not_reached.values()))
    if plan.ignored_anchors:
        notes.append(f"{_n(len(plan.ignored_anchors), 'anchor', 'anchors')} ignored: "
                     + "; ".join(sorted(set(plan.ignored_anchors.values()))))
    if plan.seams:
        notes.append(f"{_n(len(plan.seams), 'seam', 'seams')}: fronts of different anchors meet; "
                     "a disagreement there is flagged when the ring completes")

    text = (f"{_n(len(plan.anchors), 'anchor', 'anchors')} · spread in {_DIRECTION_WORDS[direction]} · "
            f"{_n(len(g.nodes), 'spectrum', 'spectra')} · {_n(len(plan.rings), 'ring', 'rings')}")
    blocked = None
    if not plan.anchors and not plan.ignored_anchors:
        blocked = "no anchor in the list"
    elif not plan.anchors:
        blocked = "no anchor can be used: " + "; ".join(sorted(set(plan.ignored_anchors.values())))
    elif n_refit == 0:
        if direction == "q" and no_q and any("q: no accepted hkl" in v for v in g.unlinked.values()):
            cause = "spreading in q needs each analyzer's accepted hkl, none is accepted for this scan"
        elif plan.not_reached:
            cause = sorted(set(plan.not_reached.values()))[0]
        else:
            cause = "no spectrum is linked to the anchors"
        blocked = f"nothing is linked to the anchors: {cause}"
    n_terms = sum(1 for l in g.links if l.a in plan.ring_of and l.b in plan.ring_of)
    return Preview(text=text, can_start=blocked is None, blocked=blocked, notes=tuple(notes),
                   n_spectra=len(g.nodes), n_refit=n_refit, n_rings=len(plan.rings),
                   n_not_reached=len(plan.not_reached), n_terms=n_terms, n_groups=0)


def preview_simultaneous(g: LinkGraph, plan: SimultaneousPlan) -> Preview:
    n_T = sum(1 for t in plan.terms if t.kind == "T-change")
    n_curv = sum(1 for t in plan.terms if t.kind == "curvature")
    text = (f"{_n(len(plan.free), 'spectrum', 'spectra')} · {_n(n_T, 'T-link', 'T-links')} · "
            f"{_n(n_curv, 'curvature term', 'curvature terms')} · {_n(len(plan.groups), 'group', 'groups')}")
    notes = list(g.notes) + _tally(Counter(plan.not_coupled.values()))
    blocked = None
    if not plan.terms:
        cause = sorted(set(plan.not_coupled.values()))[0] if plan.not_coupled else "the selection is empty"
        blocked = f"the selection has no linked spectra: {cause}"
    return Preview(text=text, can_start=blocked is None, blocked=blocked, notes=tuple(notes),
                   n_spectra=len(g.nodes), n_refit=len(plan.free), n_rings=0,
                   n_not_reached=len(plan.not_coupled), n_terms=len(plan.terms), n_groups=len(plan.groups))
