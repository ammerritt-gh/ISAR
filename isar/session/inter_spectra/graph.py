"""Links, curvature terms and the batch scope of the inter-spectra graph (C-graph). Pure, stdlib only.

Coordinates arrive as ``QPoint`` (C-q-provider); the graph never reads a file.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from isar.session.inter_spectra.layout import Grid
from isar.session.inter_spectra.nodes import NodeSet, SpectrumNode
from isar.session.inter_spectra.q_provider import NO_HKL, QPoint

Kind = Literal["T", "q"]
Direction = Literal["q", "T", "qT"]


@dataclass(frozen=True)
class Link:
    kind: Kind
    a: str; b: str                                   # spectrum keys; T: a colder, b warmer; q: a before b in grid order (row-major)
    step: float                                      # T: K (>0). q: |q_b - q_a| in its scan's q metric (rlu without a lattice epoch, inverse angstrom with one) (>0)
    dq: tuple[float, float, float] | None            # q-links: q_b - q_a in the graph's q metric, else None


@dataclass(frozen=True)
class Term:
    kind: Literal["T-change", "q-change", "curvature"]
    members: tuple[str, ...]                         # change: (a, b); curvature: (left, centre, right) along the line
    spacings: tuple[float, ...]                      # change: (step,); curvature: (|c-l|, |r-c|) in the graph's q metric
    t_frac: float | None                             # curvature only: (c-l).(r-l)/|r-l|^2, position of the centre on the chord, ~0.5 equal spacing
    line: str | None                                 # curvature only: "row 2" / "col 5"


@dataclass(frozen=True)
class LinkGraph:
    nodes: dict[str, SpectrumNode]                   # scope members (eligible or not)
    metric: dict[str, Literal["hkl", "lattice"]]     # per scan with a q-link: "lattice" when every point of that scan has cart, else "hkl"; {} with no q-link
    links: tuple[Link, ...]                          # active links: both ends in scope, both eligible; sorted by (kind, a, b)
    triplets: tuple[Term, ...]                       # curvature terms that passed the collinearity test, sorted
    unlinked: dict[str, tuple[str, ...]]             # key -> reasons it lacks a kind of link ("q: no accepted hkl", "T: only scan of its series")
    notes: tuple[str, ...]                           # summary lines for the preview (bent triplets, q pairs without curvature, repeats)

    def links_of(self, key: str, kinds: frozenset[Kind] | None = None) -> tuple[Link, ...]:
        return tuple(l for l in self.links if key in (l.a, l.b) and (kinds is None or l.kind in kinds))


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _curvature(members, pts, tol: float, line: str):
    """(Term, None) when the centre sits between the outer two and within ``tol`` of their chord; else (None, reason)."""
    p_l, p_c, p_r = pts
    d1 = [c - l for l, c in zip(p_l, p_c)]
    d2 = [r - c for c, r in zip(p_c, p_r)]
    d = [r - l for l, r in zip(p_l, p_r)]
    dd = _dot(d, d)
    if dd == 0:
        return None, "coincident points"
    t = _dot(d1, d) / dd
    perp = [x - t * y for x, y in zip(d1, d)]
    offset = math.sqrt(_dot(perp, perp) / dd)
    if not 0 < t < 1:
        return None, "folded back on itself"
    if offset > tol:
        return None, f"off the line by {100 * offset:.0f}% of its length"
    return Term("curvature", members, (math.sqrt(_dot(d1, d1)), math.sqrt(_dot(d2, d2))), t, line), None


def build_graph(nodes: Mapping[str, SpectrumNode], ns: NodeSet, *, scope: Collection[str], kinds: frozenset[Kind],
                grid: Grid | None, coords: Mapping[str, QPoint], collinearity_tol: float,
                q_reasons: Mapping[str, str] | None = None) -> LinkGraph:
    missing = [k for k in scope if k not in nodes]
    if missing:
        raise KeyError(missing[0])
    order = {k: i for i, k in enumerate(nodes)}
    members = sorted(set(scope), key=order.__getitem__)
    g_nodes = {k: nodes[k] for k in members}
    live = {k for k in members if g_nodes[k].eligible}
    scans = list(dict.fromkeys(g_nodes[k].scan for k in members))
    scan_label = {g_nodes[k].scan: g_nodes[k].label for k in members}
    series_of = {g_nodes[k].scan: g_nodes[k].series for k in members}
    by_scan_det = {(g_nodes[k].scan, g_nodes[k].detector): k for k in members}
    dets_of = defaultdict(list)
    for k in members:
        dets_of[g_nodes[k].scan].append(g_nodes[k].detector)
    notes: list[str] = []
    reasons: dict[str, list[str]] = defaultdict(list)

    # q-links: adjacent analyzers of one scan, a before b in grid (row-major) order; one metric per scan
    links, metric = [], {}
    pairs = [(a, b) for _, dets in grid.lines() for a, b in zip(dets, dets[1:])] \
        if grid is not None and "q" in kinds else []
    for scan in (scans if pairs else []):
        pts =[coords[k] for k in members if g_nodes[k].scan == scan and k in coords]
        lattice = bool(pts) and all(p.cart is not None for p in pts)
        for da, db in pairs:
            a, b = f"{scan}:{da}", f"{scan}:{db}"
            if a in live and b in live and a in coords and b in coords:
                va, vb = (coords[a].cart, coords[b].cart) if lattice else (coords[a].hkl, coords[b].hkl)
                dq = tuple(y - x for x, y in zip(va, vb))
                step = math.sqrt(_dot(dq, dq))
                if step > 0:
                    links.append(Link("q", a, b, step, dq))
                    metric[scan] = "lattice" if lattice else "hkl"
                else:
                    notes.append(f"{scan_label[scan]}: {da} and {db} have the same q, no q-link")

    # T-links: the same analyzer in adjacent scans of one series; a scan sharing its temperature with another
    # takes no link (repeat slots), and adjacency runs over all scans of the series, not only the scope
    rep_scans: set[str] = set()
    if "T" in kinds:
        for series, chain in ns.series.items():
            count = defaultdict(int)
            for s in chain:
                count[ns.slots[s]] += 1
            rep = {s for s in chain if count[ns.slots[s]] > 1}
            rep_scans |= rep
            shared = defaultdict(list)
            for s in rep:
                shared[ns.slots[s]].append(s)
            for slot, group in shared.items():
                names = [scan_label[s] for s in group if s in scan_label]
                if names:
                    notes.append(f"{' and '.join(names)} share one temperature ({slot:g} K): "
                                 "no temperature link touches them")
            for sa, sb in zip(chain, chain[1:]):
                if sa in rep or sb in rep:
                    continue
                for det in dets_of.get(sa, ()):
                    ka, kb = by_scan_det.get((sa, det)), by_scan_det.get((sb, det))
                    if ka in live and kb in live:
                        links.append(Link("T", ka, kb, g_nodes[kb].T - g_nodes[ka].T, None))
        for scan in scans:
            if series_of[scan] is None:
                notes.append(f"{scan_label[scan]}: no q setting recorded, so no temperature links")

    # curvature terms: three consecutive analyzers on a grid row or column, both q-links present
    triplets: list[Term] = []
    qlinks = {(l.a, l.b) for l in links if l.kind == "q"}
    if grid is not None and qlinks:
        for line, dets in grid.lines():
            for dl, dc, dr in zip(dets, dets[1:], dets[2:]):
                for scan in metric:
                    l, c, r = f"{scan}:{dl}", f"{scan}:{dc}", f"{scan}:{dr}"
                    if (l, c) not in qlinks or (c, r) not in qlinks:
                        continue
                    kind = metric[scan]
                    pts = [coords[x].cart if kind == "lattice" else coords[x].hkl for x in (l, c, r)]
                    term, why = _curvature((l, c, r), pts, collinearity_tol, line)
                    if term is not None:
                        triplets.append(term)
                    else:
                        notes.append(f"{line} {dl}-{dc}-{dr} ({scan_label[scan]}): {why}, no curvature term")

    # a q-connected set of two or more spectra with no curvature term is named in the notes
    adj = defaultdict(set)
    for a, b in qlinks:
        adj[a].add(b)
        adj[b].add(a)
    seen: set[str] = set()
    for k in members:
        if k in adj and k not in seen:
            comp, stack = set(), [k]
            while stack:
                x = stack.pop()
                if x not in comp:
                    comp.add(x)
                    stack.extend(adj[x] - comp)
            seen |= comp
            if not any(set(t.members) <= comp for t in triplets):
                ordered = sorted(comp, key=order.__getitem__)
                label = scan_label[g_nodes[ordered[0]].scan]
                if len(ordered) == 2:
                    notes.append(f"{g_nodes[ordered[0]].detector}-{g_nodes[ordered[1]].detector} ({label}): "
                                 "two spectra joined by one q-link, no curvature term")
                else:
                    notes.append(f"{g_nodes[ordered[0]].detector}-{g_nodes[ordered[-1]].detector} ({label}): "
                                 f"{len(ordered)} spectra joined by q-links, no curvature term")

    # why a selected spectrum takes no part in a kind of link
    touching_q = {x for pair in qlinks for x in pair}
    touching_t = {x for l in links if l.kind == "T" for x in (l.a, l.b)}
    for k in members:
        n = g_nodes[k]
        if not n.eligible:
            continue
        if "q" in kinds:
            if grid is None:
                reasons[k].append("q: no analyzer layout for this instrument")
            elif k not in coords:
                reasons[k].append(f"q: {(q_reasons or {}).get(k, NO_HKL)}")
            elif k not in touching_q:
                reasons[k].append("q: no neighbour in the selection")
        if "T" in kinds and k not in touching_t:
            if n.series is None:
                reasons[k].append("T: no q setting, so no series")
            elif n.scan in rep_scans:
                reasons[k].append("T: shares its temperature with another scan")
            elif len(ns.series.get(n.series, ())) == 1:
                reasons[k].append("T: only scan of its series")
            else:
                reasons[k].append("T: no neighbour in the selection")

    return LinkGraph(
        nodes=g_nodes, metric=metric,
        links=tuple(sorted(links, key=lambda l: (l.kind, l.a, l.b))),
        triplets=tuple(sorted(triplets, key=lambda t: t.members)),
        unlinked={k: tuple(v) for k, v in reasons.items() if v},
        notes=tuple(notes),
    )


def anchored_scope(anchors: Collection[str], direction: Direction, ns: NodeSet) -> tuple[frozenset[str], frozenset[Kind]]:
    kinds = {"q": frozenset({"q"}), "T": frozenset({"T"}), "qT": frozenset({"q", "T"})}[direction]
    by_scan = defaultdict(set)
    for k, n in ns.nodes.items():
        by_scan[n.scan].add(k)
    scope: set[str] = set()
    for a in anchors:
        n = ns.nodes.get(a)
        if n is None:
            continue                                 # an unknown anchor: the plan reports it
        series_scans = ns.series.get(n.series, (n.scan,)) if n.series else (n.scan,)
        if direction == "q":
            scope |= by_scan[n.scan]
        elif direction == "T":
            scope |= {f"{s}:{n.detector}" for s in series_scans}
        else:
            scope |= {k for s in series_scans for k in by_scan[s]}
    return frozenset(k for k in scope if k in ns.nodes), kinds
