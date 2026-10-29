"""The simultaneous plan: free and held spectra, terms, conflict-free parallel groups (C-graph).

Pure, stdlib only.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from isar.session.inter_spectra.graph import LinkGraph, Term
from isar.session.inter_spectra.q_provider import INVALID_CELL, NO_HKL


@dataclass(frozen=True)
class SimultaneousPlan:
    free: tuple[str, ...]                            # refitted; selection, eligible, unlocked, in >= 1 term
    held: tuple[str, ...]                            # locked members of the terms (fixed neighbours)
    terms: tuple[Term, ...]                          # T-change per T-link + curvature triplets; each has >= 1 free member
    terms_of: dict[str, tuple[int, ...]]             # free key -> indices into terms (the refit includes ALL of them)
    groups: tuple[tuple[str, ...], ...]              # conflict-free parallel groups, run in this order within a sweep
    not_coupled: dict[str, str]                      # selected keys that take no part -> reason


def _alone(g: LinkGraph, k: str) -> str:
    """Why an eligible selected spectrum forms no term: its q-linked set has no curvature term, or it lacks an hkl."""
    if g.links_of(k, frozenset({"q"})):
        seen, stack = {k}, [k]                       # the q-connected set k belongs to
        while stack:
            x = stack.pop()
            for link in g.links_of(x, frozenset({"q"})):
                y = link.b if link.a == x else link.a
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return "its q-linked set has no curvature term" + (" (two spectra)" if len(seen) == 2 else "")
    why = g.unlinked.get(k, ())
    if "q: no analyzer layout for this instrument" in why:
        return "this instrument has no analyzer layout"
    if f"q: {INVALID_CELL}" in why:
        return INVALID_CELL
    if f"q: {NO_HKL}" in why:
        return "no accepted hkl for its analyzer"
    return "no neighbour in the selection"


def plan_simultaneous(g: LinkGraph, locks: Collection[str]) -> SimultaneousPlan:
    order = {k: i for i, k in enumerate(g.nodes)}
    locked = {k for k in locks if k in g.nodes}
    terms = [Term("T-change", (l.a, l.b), (l.step,), None, None) for l in g.links if l.kind == "T"]
    terms = [t for t in terms + list(g.triplets) if any(m not in locked for m in t.members)]

    free_set = {m for t in terms for m in t.members if m not in locked}
    free = tuple(sorted(free_set, key=order.__getitem__))
    held = tuple(sorted({m for t in terms for m in t.members if m in locked}, key=order.__getitem__))
    terms_of = {s: tuple(i for i, t in enumerate(terms) if s in t.members) for s in free}

    not_coupled = {}
    for k, n in g.nodes.items():
        if k in free_set or k in locked:
            continue
        not_coupled[k] = _alone(g, k) if n.eligible else f"ineligible: {n.reason or 'not eligible'}"

    # two free spectra conflict when a term holds both; a refit reads every other member of its terms
    sets = {s: set(terms_of[s]) for s in free}
    conflict = {s: {t for t in free if t != s and sets[s] & sets[t]} for s in free}
    colour: dict[str, int] = {}
    for s in sorted(free, key=lambda s: (-len(conflict[s]), s)):      # Welsh-Powell, deterministic
        taken = {colour[t] for t in conflict[s] if t in colour}
        colour[s] = next(c for c in range(len(taken) + 1) if c not in taken)
    n_colours = max(colour.values()) + 1 if colour else 0
    groups = tuple(tuple(sorted((s for s in free if colour[s] == c), key=order.__getitem__))
                   for c in range(n_colours))

    return SimultaneousPlan(free=free, held=held, terms=tuple(terms), terms_of=terms_of,
                            groups=groups, not_coupled=not_coupled)


def check_groups(plan: SimultaneousPlan) -> None:          # raises AssertionError naming the first violation
    placed = [s for group in plan.groups for s in group]
    if sorted(placed) != sorted(plan.free):
        raise AssertionError(f"groups do not partition the free spectra: {len(placed)} placed, {len(plan.free)} free")
    where = {s: gi for gi, group in enumerate(plan.groups) for s in group}
    for i, t in enumerate(plan.terms):
        for gi in set(where.get(m) for m in t.members) - {None}:
            inside = [m for m in t.members if where.get(m) == gi]
            if len(inside) > 1:
                raise AssertionError(f"group {gi} holds both {inside[0]} and {inside[1]} of term {i} ({t.kind})")
    for s in plan.free:
        want = tuple(i for i, t in enumerate(plan.terms) if s in t.members)
        if plan.terms_of.get(s) != want:
            raise AssertionError(f"terms_of[{s}] is {plan.terms_of.get(s)}, expected {want}")
