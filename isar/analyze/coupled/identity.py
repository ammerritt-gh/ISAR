"""Persistent mode identity across neighbours (C-identity). Stdlib only; never prices (S1U2 does).

Branch ids live apart from the energy order: a mode is located by a ``ModeRef`` (spectrum key, immutable
fit id, index) and named by a persistent branch id that is minted once, at a confirmed birth
(``new_branch_id``), and carried thereafter. Three calls:

- ``match_link``: ranked distinct proposals (compositions) of local modes against held branches, from
  costs the core supplies; no evidence weight is invented here.
- ``propagate_ids``: at a stage boundary (after a ring's or a round's assignments), resolve every
  stage spectrum's slot ids from all eligible neighbours at once. Correspondences enter a union only
  when supported; a component that would hold two modes of one fit is a loop conflict, reported with
  a witness and left ambiguous whatever order its edges arrive in. A held branch not matched locally
  is "not observed here", never a death; a return keeps its id only over an eligible graph path.
- ``drift_report``: cumulative T drift per (analyzer, branch) segment, an endpoint diagnostic on the
  core's unlevelled scale; it never becomes a fitted factor.
"""
from __future__ import annotations

import itertools
import math
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass

from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import SEVERITY_OF

ModeRef = tuple[str, str, int]     # spectrum key, immutable fit_id, sorted index in THAT fit

# Node ceiling of match_link's branch-and-bound; reaching it is recorded as enumeration_truncated.
_NODE_LIMIT = 200_000


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


def new_branch_id(fit: str, index: int) -> str:
    """The id of a branch born as mode ``index`` of fit ``fit``: ``"<fit>#<index>"``.

    A fast solo's modes are named ``new_branch_id(f"fast:{unit}", i)``; the bare ``"fast"`` is shared by
    every spectrum, so it is refused rather than minting one id for many births."""
    if fit == "fast":
        raise ValueError('a fast solo is named by "fast:<unit>", not the shared id "fast"')
    return f"{fit}#{index}"


# ---- match_link ---------------------------------------------------------------------------------
def _composition(slots: Sequence[str | None]) -> tuple[tuple[str, ...], int]:
    return tuple(sorted(b for b in slots if b is not None)), sum(b is None for b in slots)


def _order_key(slots: Sequence[str | None]) -> list:
    return [(b is not None, b or "") for b in slots]     # None sorts first; a tie keeps one winner


def match_link(local: Sequence[ModeObs], branches: Sequence[str], proposal_costs, incoming=None,
               keep: int | None = 4, close: float = 0.0) -> dict:
    """Ranked distinct compositions of ``local`` against the held ``branches`` (search only).

    ``proposal_costs(m, b)`` is the core's cost in deviance units of local mode ``m`` taking branch ``b``
    (every incident factor once), or of staying unmatched (``b is None``: the entry price); ``inf``
    forbids. A held branch absent locally costs nothing more. ``branches`` is deduplicated: duplicate
    observations of one branch are one assignable branch. ``incoming`` is the incoming slots tuple
    (branch or None per local mode) relative to ``branches``. ``keep=None`` ranks every composition
    (full search). ``close`` is the settings' ``close_call_nats``.

    Returns ``{"proposals": [{"slots", "composition", "cost", "source", "orders"}], "coverage": {...},
    "weight_unavailable": True, "incoming_invalid": reason or None}``: the incoming and all-unmatched
    proposals always, then up to ``keep`` other distinct compositions, each with its cheapest
    association, cheapest first. ``orders`` lists every slot order of that composition within ``close``
    of its cheapest, so a competing identity (an exchange at a crossing) reaches the core to price.
    Costs rank proposals; they are not evidence weights.
    """
    pool = list(dict.fromkeys(branches))
    n = len(local)
    options = [None, *pool]
    cost = [[float(proposal_costs(m, b)) for b in options] for m in range(n)]
    rest = [sum(min(row) for row in cost[m:]) for m in range(n + 1)]   # admissible bound of open modes

    incoming_invalid = None
    mandatory = []
    if incoming is not None:
        incoming = tuple(incoming)
        named = [b for b in incoming if b is not None]
        if len(incoming) != n:
            incoming_invalid = f"incoming has {len(incoming)} slots for {n} local modes"
        elif any(b not in pool for b in named):
            incoming_invalid = "incoming names a branch outside the pool"
        elif len(set(named)) != len(named):
            incoming_invalid = "incoming names one branch twice"
        else:
            mandatory.append((incoming, "incoming"))
    if not mandatory or mandatory[0][0] != (None,) * n:
        mandatory.append(((None,) * n, "all_unmatched"))
    fixed = {_composition(s) for s, _ in mandatory}

    best: dict[tuple, tuple[float, tuple]] = {}         # composition -> (cost, slots) of its cheapest association
    leaves: dict[tuple, dict[tuple, float]] = defaultdict(dict)   # ranked composition -> {slots: cost}, within bound
    state ={"nodes": 0, "truncated": False, "limit": -math.inf if keep == 0 else math.inf}

    def record(slots: tuple, c: float) -> None:
        comp = _composition(slots)
        leaves[comp][slots] = c
        if comp not in best or (c, _order_key(slots)) < (best[comp][0], _order_key(best[comp][1])):
            best[comp] = (c, slots)
        if keep:
            others = sorted(v[0] for k, v in best.items() if k not in fixed)
            if len(others) >= keep:
                state["limit"] = others[keep - 1]

    def walk(m: int, slots: list, used: set, partial: float) -> None:
        state["nodes"] += 1
        if state["nodes"] > _NODE_LIMIT:
            state["truncated"] = True
            return
        if m == n:
            record(tuple(slots), partial)
            return
        for j, b in enumerate(options):
            if state["truncated"]:
                return
            c = partial + cost[m][j]
            bound = state["limit"] + close          # a kept ranked order is within close of a kept (<= limit) best
            if (b is not None and b in used) or not math.isfinite(c) or c + rest[m + 1] > bound:
                continue
            slots.append(b)
            used.add(b)
            walk(m + 1, slots, used, c)
            slots.pop()
            used.discard(b)

    walk(0, [], set(), 0.0)

    def slot_cost(s: Sequence) -> float:
        return sum(cost[m][options.index(b)] for m, b in enumerate(s))

    def orders(found: dict[tuple, float], slots: tuple, c: float) -> list[dict]:
        finite = {s: v for s, v in found.items() if math.isfinite(v)}
        if not finite:                                  # nothing finite to keep: the proposal keeps its own slots
            return [{"slots": slots, "cost": c}]
        low = min(finite.values())
        return [{"slots": s, "cost": v} for s, v in sorted(finite.items(), key=lambda kv: (kv[1], _order_key(kv[0])))
                if v <= low + close]

    def composition_orders(slots: Sequence) -> dict[tuple, float]:
        # shortcut: every order of a fixed composition by permutation of its labels, n! for n local modes;
        # upgrade to branch and bound if n ever exceeds about 8
        names, unmatched = _composition(slots)
        labels = [*names, *([None] * unmatched)]
        return {p: slot_cost(p) for p in set(itertools.permutations(labels))}

    ranked = sorted((c, comp, s) for comp, (c, s) in best.items() if comp not in fixed)
    kept = ranked if keep is None else ranked[:keep]
    proposals = []
    for s, src in mandatory:
        c = slot_cost(s)
        proposals.append({"slots": s, "composition": _composition(s), "cost": c, "source": src,
                          "orders": orders(composition_orders(s), s, c)})
    proposals += [{"slots": s, "composition": comp, "cost": c, "source": "ranked",
                   "orders": orders(leaves[comp], s, c)} for c, comp, s in kept]
    total = sum(math.comb(len(pool), r) for r in range(min(n, len(pool)) + 1))
    returned = len({p["composition"] for p in proposals})
    return {"proposals": proposals,
            "coverage": {"compositions_total": total, "compositions_returned": returned,
                         "enumeration_truncated": state["truncated"],
                         "search_limited": state["truncated"] or returned < total},
            "weight_unavailable": True, "incoming_invalid": incoming_invalid}


# ---- propagate_ids ------------------------------------------------------------------------------
class _UnionFind:
    def __init__(self):
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _obs(ref: ModeRef) -> tuple:
    return ("obs", ref[0], ref[1], ref[2])


def _ref(node: tuple) -> ModeRef:
    return node[1], node[2], node[3]


def _slot_support(a, m: int, settings: InterSettings) -> tuple[bool, list[dict]]:
    """(confident, close alternatives) of slot m from the core's evaluated candidates at the chosen count.

    Confident when the conditional mass of the chosen id at m reaches ``confidence``. Candidates within
    ``close_call_nats`` of the winner that name another id there are returned for review only, as in
    the installed decision rule: a close call is not ambiguity by itself. A skipped stage is inherited:
    its decision stands with its old scope, and no new weight is claimed for it."""
    if a.discrete == "skipped":
        return True, []
    cands = [c for c in a.candidates if c.get("count") == a.count and c.get("ln_z") is not None
             and math.isfinite(c["ln_z"])]
    win = [c for c in cands if tuple(c["slots"]) == tuple(a.slots)]
    if not win:
        return False, [dict(c) for c in cands]
    top = max(c["ln_z"] for c in cands)
    w = [math.exp(c["ln_z"] - top) for c in cands]
    p = sum(wi for wi, c in zip(w, cands) if c["slots"][m] == a.slots[m]) / sum(w)
    close = [dict(c) for c in cands if c["ln_z"] >= win[0]["ln_z"] - settings.close_call_nats
             and c["slots"][m] != a.slots[m]]
    return p >= settings.confidence, close


def _carried_ids(a, own, energy: Sequence[float]) -> dict[int, str]:
    """Unmatched slot m -> the branch of the incoming mode it continues: a one-to-one pairing, nearest in
    energy first, of the assignment's unmatched modes with the incoming modes no slot names. A mode left
    without a partner is a birth."""
    used = {b for b in a.slots if b is not None}
    free = [j for j, md in enumerate(own.modes) if md.branch not in used]
    pairs = sorted((abs(energy[m] - own.modes[j].omega0), m, j)
                   for m, b in enumerate(a.slots) if b is None for j in free)
    paired_m, paired_j, carried = set(), set(), {}
    for _, m, j in pairs:
        if m not in paired_m and j not in paired_j:
            paired_m.add(m)
            paired_j.add(j)
            carried[m] = own.modes[j].branch
    return carried


def _flag(flags: dict, code: str, units, refs=(), **values) -> None:
    entry = flags.setdefault(code, {"severity": SEVERITY_OF[code], "units": set(), "ids": set(), "values": []})
    entry["units"].update(units)
    entry["ids"].update(refs)
    if values:
        entry["values"].append(values)


def _close_flags(flags: dict) -> dict:
    for entry in flags.values():
        entry["units"] = sorted(entry["units"])
        entry["ids"] = sorted(entry["ids"])
    return flags


def propagate_ids(order, links, modes, settings: InterSettings, *, batch_mode, rings, snapshot_id,
                  assignments, previous, energies) -> IdentityResult:
    """Resolve the display ids of one stage's spectra from all eligible neighbours at once.

    order: the stage's spectrum keys (any order; the result never depends on it).
    links: the graph's active links (objects with ``a`` and ``b`` spectrum keys).
    modes: stage key -> the ModeRefs of its assignment's slots, ``(key, Snapshot.fit_id, m)`` for
        m in range(count); the refit keeps each slot's id with its labelled coordinates.
    batch_mode: "anchored" | "simultaneous"; rings: key -> ring (anchored, ``plan.ring_of``) or None.
    snapshot_id: the stage's snapshot; every assignment must have been decided on it.
    assignments: stage key -> Assignment (C-snapshot). previous: key -> Held, the state frozen at the
        stage start (anchored: anchors, valid locks and earlier rings; simultaneous: every member).
    energies: stage key -> the solved omega0 of each slot, in ``modes[key]`` order (the seam gate's only input).

    Support for a stage spectrum's slot is a linked held neighbour carrying the branch (or a dormant
    record of it) outside the stage; anchored, a ring-mate never supports (its matches are seams only).
    A slot of an unchanged assignment continues its own incoming mode at the same index (bookkeeping,
    simultaneous); an unmatched slot of a changed one keeps the id of the incoming mode nearest in energy
    that no slot names (one-to-one; a mode with none left is a birth). ``drift`` stays empty here
    (``drift_report`` runs at batch end). Nothing here prices, adds a factor or changes an assignment.
    """
    if batch_mode not in ("anchored", "simultaneous"):
        raise ValueError(f"unknown batch mode {batch_mode!r}")
    stage = sorted(set(order))
    stage_set = set(stage)
    if batch_mode == "anchored":
        if rings is None:
            raise ValueError("an anchored stage needs its rings")
        if len({rings.get(k) for k in stage}) > 1:
            raise ValueError("an anchored stage holds one ring only")
    neighbours: dict[str, set[str]] = defaultdict(set)
    for link in links:
        neighbours[link.a].add(link.b)
        neighbours[link.b].add(link.a)

    def eligible(k: str) -> list[str]:
        out = [j for j in neighbours[k] if j in previous and j != k]
        if batch_mode == "anchored":
            out = [j for j in out if j not in stage_set]
        return sorted(out)

    ids: dict = {}
    status: dict = {}
    flags: dict = {}
    alternatives: dict = {"by_unit": {}, "aliases": {}, "invalid": {}}
    absent: dict[str, list[dict]] = {}

    valid: list[str] = []
    for k in stage:
        a = assignments.get(k)
        refs = tuple(modes.get(k, ()))
        if a is None:
            reason = "no assignment for this stage"
        elif a.unit != k or a.snapshot_id != snapshot_id:
            reason = f"assignment of {a.unit} decided on {a.snapshot_id}, not {k} on {snapshot_id}"
        elif len(refs) != a.count or len(a.slots) != a.count or len(a.origins) != a.count:
            reason = "slots, origins and mode references disagree in number"
        elif any(r[0] != k or r[2] != m for m, r in enumerate(refs)) or len({r[1] for r in refs}) > 1:
            reason = "mode references do not name this spectrum's one fit by slot"
        else:
            valid.append(k)
            continue
        alternatives["invalid"][k] = reason            # a diagnostic, no new authoritative id

    # Graph of supported correspondences over observation and id nodes: a held observation is joined to
    # the id it carries; a confident, supported slot to the id it names; an unchanged slot to its own
    # incoming mode. Every edge enters, so the components do not depend on the order of the edges.
    uf = _UnionFind()
    edges: dict[tuple, list[dict]] = defaultdict(list)

    def edge(x: tuple, y: tuple, **why) -> None:
        uf.union(x, y)
        edges[x].append({"to": y, **why})
        edges[y].append({"to": x, **why})

    for u in sorted(previous):
        h = previous[u]
        for j, md in enumerate(h.modes):
            edge(_obs((u, h.fit, j)), ("id", md.branch), kind="carries")

    plan: dict[ModeRef, dict] = {}                     # per stage slot: what its resolution rests on
    for k in valid:
        a, refs, elig = assignments[k], tuple(modes[k]), eligible(k)
        own = previous.get(k)
        carried = _carried_ids(a, own, energies[k]) if own is not None else {}
        continues = own is not None and not a.changed and len(own.modes) == a.count
        inherited = continues and a.discrete == "skipped"    # an untriggered decision is not re-tested for support
        for m, ref in enumerate(refs):
            b, origin = a.slots[m], a.origins[m]
            confident, close = _slot_support(a, m, settings)
            via = sorted((k, j) for j in elig if any(md.branch == b for md in previous[j].modes)
                         or any(d.branch == b for d in previous[j].dormant))
            supported = b is not None and bool(via)
            kept = carried.get(m)
            plan[ref] = {"slot": b, "origin": origin, "confident": confident, "close": close,
                         "supported": supported, "inherited": inherited, "kept": kept,
                         "own": own.modes[m].branch if continues else None}
            if not confident or origin == "unresolved":
                continue
            if supported:
                edge(_obs(ref), ("id", b), kind="assigned", links=via)
            if continues and (b is None or supported or inherited):
                edge(_obs(ref), _obs((k, own.fit, m)), kind="continuity", links=[])

    members: dict = defaultdict(list)
    for node in list(uf.parent):
        members[uf.find(node)].append(node)
    conflict: dict = {}                                # root -> the first colliding pair of one fit
    for root, nodes in members.items():
        seen: dict[tuple[str, str], tuple] = {}
        pairs = []
        for node in sorted(x for x in nodes if x[0] == "obs"):
            if (node[1], node[2]) in seen:
                pairs.append((seen[node[1], node[2]], node))
            else:
                seen[node[1], node[2]] = node
        if pairs:
            conflict[root] = min(pairs)

    def witness(x: tuple, y: tuple) -> list[dict]:
        back = {x: None}
        queue = deque([x])
        while queue and y not in back:
            node = queue.popleft()
            for e in sorted(edges[node], key=lambda e: (e["to"], e["kind"])):
                if e["to"] not in back:
                    back[e["to"]] = (node, e)
                    queue.append(e["to"])
        path, node = [], y
        while back.get(node) is not None:
            prev, e = back[node]
            path.append({"from": prev, "to": node, "kind": e["kind"], "links": e.get("links", [])})
            node = prev
        return path[::-1]

    loops = []
    for root in sorted(conflict, key=lambda r: conflict[r]):
        x, y = conflict[root]
        stage_refs = sorted(_ref(n) for n in members[root] if n[0] == "obs" and _ref(n) in plan)
        competing = sorted(n[1] for n in members[root] if n[0] == "id")
        loops.append({"collision": [_ref(x), _ref(y)], "witness": witness(x, y),
                      "competing": competing, "modes": stage_refs})
        _flag(flags, "loop_inconsistent", {r[0] for r in stage_refs} | {x[1]}, stage_refs,
              collision=[_ref(x), _ref(y)], competing=competing)

    fixed_ids = {md.branch for u, h in previous.items() if u not in stage_set for md in h.modes}
    names: dict = {}
    for root in sorted(set(members) - set(conflict)):
        found = sorted(n[1] for n in members[root] if n[0] == "id")
        if found:
            names[root] = min(found, key=lambda b: (b not in fixed_ids, b))   # held outside the stage first
            if len(found) > 1:
                alternatives["aliases"][names[root]] = [b for b in found if b != names[root]]

    for k in valid:
        a, refs = assignments[k], tuple(modes[k])
        for m, ref in enumerate(refs):
            info = plan[ref]
            b, origin = info["slot"], info["origin"]
            root = uf.find(_obs(ref))
            if root in conflict:
                ids[ref], status[ref] = info["own"], "ambiguous"
            elif origin == "unresolved" or (origin == "returning" and not info["supported"]):
                ids[ref], status[ref] = None, "unresolved_return"
                _flag(flags, "unresolved_return", {k}, [ref], slot=b,
                      reason="no eligible graph path supports the return")
            elif not info["confident"] or (b is not None and not info["supported"] and not info["inherited"]):
                ids[ref], status[ref] = info["own"], "ambiguous"
                why = "close alternatives" if not info["confident"] else "no eligible neighbour carries it"
                _flag(flags, "identity_ambiguous", {k}, [ref], slot=b, reason=why)
            elif b is not None:
                ids[ref], status[ref] = names[root], "matched"
            else:
                ids[ref], status[ref] = names.get(root) or info["kept"] or new_branch_id(ref[1], m), "new_branch"

        resolved = {ids[r] for r in refs} | {plan[r]["slot"] for r in refs}
        recs = []
        for j in eligible(k):
            h = previous[j]
            for md in h.modes:
                name = names.get(uf.find(("id", md.branch)), md.branch)
                if md.branch not in resolved and name not in resolved:
                    recs.append({"branch": name, "last_seen_fit": h.fit, "last_seen_unit": j,
                                 "status": "not_observed_here"})
            for d in h.dormant:
                if d.branch not in resolved:
                    recs.append({"branch": d.branch, "last_seen_fit": d.last_seen_fit,
                                 "last_seen_unit": d.last_seen_unit, "status": "not_observed_here"})
        by_branch: dict[str, dict] = {}
        for r in sorted(recs, key=lambda r: (r["branch"], r["last_seen_unit"], r["last_seen_fit"])):
            by_branch.setdefault(r["branch"], r)
        if by_branch:
            absent[k] = list(by_branch.values())

        factor_ids = defaultdict(list)
        for f in a.factors:
            factor_ids[getattr(f, "branch_id", None)].append(getattr(f, "factor_id", None))
        pool = sorted({md.branch for j in eligible(k) for md in previous[j].modes})
        matches = tuple(Match(branch=p, local=next((r for r in refs if plan[r]["slot"] == p), None),
                              term_ids=tuple(sorted(factor_ids.get(p, ())))) for p in pool)
        alternatives["by_unit"][k] = {
            "snapshot_id": a.snapshot_id, "discrete": a.discrete,
            "weights": "inherited from the incoming decision" if a.discrete == "skipped"
                       else "conditional on the evaluated candidates",
            "selected": {"count": a.count, "slots": tuple(a.slots), "origins": tuple(a.origins)},
            "close": {m: plan[r]["close"] for m, r in enumerate(refs) if plan[r]["close"]},
            "coverage": dict(a.coverage), "search_limited": a.search_limited, "matches": matches}

    seams = []
    if batch_mode == "anchored":
        done = set(valid)
        pairs = sorted({tuple(sorted((link.a, link.b))) for link in links})
        for pair in pairs:
            if not (pair[0] in done and pair[1] in done):
                continue
            unsure = sorted(r for k in pair for r in modes[k] if status[r] in ("ambiguous", "unresolved_return"))
            born = sorted(r for k in pair for r in modes[k]
                          if status[r] == "new_branch" and ids[r] == new_branch_id(r[1], r[2]))
            if unsure:
                seams.append({"link": pair, "issue": "identity_unresolved", "modes": unsure})
            tol = settings.q_tolerance_meV + settings.q_change_scale_meV
            near = any(abs(energies[x[0]][x[2]] - energies[y[0]][y[2]]) <= tol
                       for x in born for y in born if x[0] < y[0])
            if born and {r[0] for r in born} == set(pair) and near:
                # two ring-mates each minted a branch that could be one mode under two ids; never merged here
                seams.append({"link": pair, "issue": "independent_births", "modes": born,
                              "ids": [ids[r] for r in born]})
                _flag(flags, "seam_disagreement", set(pair), born, link=list(pair), issue="independent_births")

    return IdentityResult(ids=ids, status=status, alternatives=alternatives, absent=absent, loops=loops,
                          drift={}, seams=seams, flags=_close_flags(flags))


# ---- drift_report -------------------------------------------------------------------------------
def _t_unlevelled(dE: float, dT: float, g0: float, g1: float, s: InterSettings) -> tuple[float, float]:
    """(s_eff, U) of a T change on the core's unlevelled scale (spec 1 and 6), independent guards."""
    d = abs(dT) / s.reference_T_K
    base = s.t_tolerance_meV if s.t_spacing_form == "divide" else s.t_tolerance_meV * d ** s.step_exponent
    s_eff = math.sqrt(base * base + g0 * g0 + g1 * g1)
    z = abs(dE) / s_eff
    U = s.penalty_cap * z ** 4 if s.shape_family == "quartic" else 0.5 * s.penalty_cap * z * z
    return s_eff, (U / d if s.t_spacing_form == "divide" else U)


def drift_report(segments, settings: InterSettings) -> dict:
    """Cumulative T drift of each (analyzer, branch) segment: an endpoint diagnostic, never a factor.

    segments: iterable of ``{"analyzer", "branch", "observations": [{"unit", "T", "omega0", "guard",
    "status"}]}``; ``omega0`` None marks the branch not observed there (a gap); ``guard`` None is an
    unavailable endpoint guard (the diagnosis is "qualified", the guard taken as 0); a ``status`` of
    "ambiguous" or "unresolved_return" makes the segment unavailable. The end-to-end change telescopes
    the steps, so it is one diagnostic, not added evidence. Returns ``{"segments": [...], "flags":
    {...}}``; a segment is flagged when its endpoint U passes ``flag_threshold``.
    """
    out, flags = [], {}
    for seg in segments:
        obs = sorted(seg["observations"], key=lambda o: (o["T"], o["unit"]))
        seen = [o for o in obs if o.get("omega0") is not None]
        rec = {"analyzer": seg["analyzer"], "branch": seg["branch"], "units": [o["unit"] for o in seen],
               "n_obs": len(seen), "gaps": len(obs) - len(seen), "flagged": False}
        if any(o.get("status") in ("ambiguous", "unresolved_return") for o in obs):
            out.append({**rec, "status": "unavailable", "reason": "identity ambiguous along the segment"})
            continue
        if len({o["T"] for o in seen}) < 2:
            out.append({**rec, "status": "unavailable", "reason": "fewer than two distinct temperatures"})
            continue
        lo, hi = seen[0], seen[-1]
        dT, dE = hi["T"] - lo["T"], hi["omega0"] - lo["omega0"]
        missing = lo.get("guard") is None or hi.get("guard") is None
        s_eff, U = _t_unlevelled(dE, dT, lo.get("guard") or 0.0, hi.get("guard") or 0.0, settings)
        steps = [_t_unlevelled(b["omega0"] - a["omega0"], b["T"] - a["T"], a.get("guard") or 0.0,
                               b.get("guard") or 0.0, settings)[1]
                 for a, b in zip(seen, seen[1:]) if b["T"] != a["T"]]
        rec.update(status="qualified" if missing else "ok",
                   reason="an endpoint guard is unavailable, taken as 0" if missing else None,
                   T_start=lo["T"], T_end=hi["T"], dT=dT, dE=dE, s_eff=s_eff, z=abs(dE) / s_eff, U=U,
                   max_step_U=max(steps, default=0.0), flagged=U > settings.flag_threshold)
        out.append(rec)
        if rec["flagged"]:
            _flag(flags, "t_drift_cumulative", rec["units"], (), analyzer=seg["analyzer"],
                  branch=seg["branch"], dE=dE, dT=dT, U=U)
    return {"segments": out, "flags": _close_flags(flags)}
