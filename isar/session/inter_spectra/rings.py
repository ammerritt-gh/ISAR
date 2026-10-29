"""Rings, seams and the anchored plan (C-graph). Pure, stdlib only; fixed by distance before anything runs.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass

from isar.session.inter_spectra.graph import Link, LinkGraph
from isar.session.inter_spectra.q_provider import INVALID_CELL, NO_HKL


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


def _other(link: Link, v: str) -> str:
    return link.b if link.a == v else link.a


def plan_anchored(g: LinkGraph, anchors: Collection[str], locks: Collection[str],
                  stale_locks: Collection[str] = ()) -> AnchoredPlan:
    order = {k: i for i, k in enumerate(g.nodes)}
    locked = {k for k in locks if k in g.nodes}
    stale = {k for k in stale_locks if k in locked}          # a stale locked fit is reached but never conditions
    adj = defaultdict(list)
    for l in g.links:
        adj[l.a].append(l)
        adj[l.b].append(l)

    ignored: dict[str, str] = {}
    picked = set()
    for a in anchors:
        n = g.nodes.get(a)
        if n is None:
            ignored[a] = "not in the batch's scope"
        elif not n.eligible:
            ignored[a] = n.reason or "not eligible"
        else:
            picked.add(a)
    sources = sorted(picked, key=order.__getitem__)

    # ring = distance in links from the nearest anchor; locks are reached like any node but are not sources
    ring_of = {a: 0 for a in sources}
    levels = [sources]
    while levels[-1]:
        nxt = set()
        for v in levels[-1]:
            for l in adj[v]:
                u = _other(l, v)
                if u not in ring_of:
                    ring_of[u] = len(levels)
                    nxt.add(u)
        levels.append(sorted(nxt, key=order.__getitem__))

    # which anchors are nearest, ties kept; computed before any pruning, so it is pure distance
    fronts = {a: (a,) for a in sources}
    for r in range(1, len(levels) - 1):
        for u in levels[r]:
            fronts[u] = tuple(sorted({a for l in adj[u] if ring_of.get(_other(l, u)) == r - 1
                                      for a in fronts[_other(l, u)]}))

    # a member with no fitted neighbour left is not reached; ring by ring, so its own outward neighbours follow
    rings, not_reached = [], {}
    for r in range(1, len(levels) - 1):
        members, conds, locked_here = [], {}, []
        for v in levels[r]:
            if v in locked:
                locked_here.append(v)
                continue
            cs = tuple(l for l in adj[v] if _other(l, v) not in stale
                       and (_other(l, v) in locked or ring_of.get(_other(l, v), r) < r))
            if cs:
                members.append(v)
                conds[v] = cs
            else:
                cut_off = any(_other(l, v) in stale for l in adj[v])
                not_reached[v] = ("only fitted neighbour is a stale locked fit" if cut_off
                                  else "no fitted neighbour left in the batch")
                del ring_of[v]
        rings.append(Ring(r, tuple(members), tuple(locked_here), conds))

    seams = tuple(sorted(
        (Seam(ring_of[l.a], l, (fronts[l.a], fronts[l.b]))
         for l in g.links if l.a in ring_of and l.b in ring_of and ring_of[l.a] == ring_of[l.b] >= 1),
        key=lambda s: (s.ring, s.link.a, s.link.b)))

    for k, n in g.nodes.items():
        if k in ring_of or k in not_reached:
            continue
        if not n.eligible:
            not_reached[k] = n.reason or "not eligible"
        elif f"q: {NO_HKL}" in g.unlinked.get(k, ()) and not adj[k]:
            not_reached[k] = NO_HKL
        elif f"q: {INVALID_CELL}" in g.unlinked.get(k, ()) and not adj[k]:
            not_reached[k] = INVALID_CELL
        else:
            not_reached[k] = "no path to an anchor within the scope"

    return AnchoredPlan(
        anchors=tuple(sources), ignored_anchors=ignored, ring_of=ring_of,
        fronts={k: fronts[k] for k in ring_of}, rings=tuple(rings), seams=seams, not_reached=not_reached,
    )
