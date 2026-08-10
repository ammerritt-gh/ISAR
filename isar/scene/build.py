"""Observations → a cluster-factorized, exactly-scored scene revision (SCENE-3 PR 3a).

This is the arithmetic of SCENE_DESIGN §2b and nothing else. For a cluster of observation
arenas, an alternative ``S`` picks exactly one surviving candidate per arena, and

    ln W(S) = Σ_j lnZ(j, h_j(S)) + Σ_f ln φ_f(S),   P(S|D) = exp(ln W(S) − lse)

normalized *within the cluster*. The invariants the code is written to keep, each naming the
failure it prevents:

* **One arena, one lnZ.** ``base`` sums one candidate's ``log_evidence`` per arena. There is
  no per-component evidence arithmetic anywhere in this module — a multimode candidate's
  modes are separate *components* (they can be assigned to different phenomena) but were
  fitted jointly to the same counts, so splitting their evidence would manufacture evidence.
* **Normalized posteriors are never multiplied.** ``CandidateRow.posterior_probability`` is
  read nowhere here; only ``log_evidence`` is summed.
* **No evidence, no term.** An arena with no usable finite ``lnZ`` contributes nothing to any
  score, so it is experiment-map-only: its footprint is recorded, it joins no cluster.
* **Disconnected clusters are independent.** Normalization is per cluster, so adding an
  unrelated arena cannot move an existing cluster's posteriors by a single bit.
* **Byte-identical replay.** The revision carries no timestamp and no machine identity; every
  ordering is derived from the data (scan_ref, then label), so two builds of the same inputs
  produce the same digest.

Stdlib-only and pure: no filesystem, no ``isar.session``/``analyze``/``drive``.
"""
from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, replace

from .factors import FACTORS, registry_fingerprint, validate_factor
from .model import (CandidateRow, ComponentObservation, Footprint, SceneObservation,
                    Visibility, fingerprint, footprint_of)

SCENE_SCHEMA = "isar.scene-revision/1"

# Same currency and idiom as isar/analyze/evidence.py PRUNE_MARGIN: nats below the cluster's
# per-arena leader, beyond which a candidate's posterior mass is bounded by e^−margin and it
# stops being a contender. Owner-tunable.
SCENE_PRUNE_MARGIN = 100.0

# Hard cap on alternatives enumerated per cluster. Owner-tunable; a truncated cluster says so
# and carries a stated bound on the mass it dropped.
SCENE_ALT_CAP = 64


# ----------------------------------------------------------------------------
# What a factor is allowed to see
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SceneAlternative:
    """One coherent per-cluster assignment, as a factor sees it.

    Carries no evidence: ``base`` lives in the builder's ledger, never in the object handed
    to ``score``, so a factor cannot re-weight the local evidence it is forbidden to touch.
    """

    cluster_id: str
    assignment: tuple[tuple[str, str], ...]          # (scan_ref, chosen label), by scan_ref
    components: tuple[ComponentObservation, ...]     # components of the chosen candidates only


@dataclass(frozen=True, slots=True)
class ClusterView:
    """The cluster context handed to ``score`` alongside the alternative.

    ``components`` holds every arena's components (not only the chosen ones) so a factor can
    reason about what *could* have been there; ``footprints`` is the experiment map, which is
    how a factor prices a coverage gap without inventing an update for it.
    """

    cluster_id: str
    scan_refs: tuple[str, ...]
    components: dict[str, tuple[ComponentObservation, ...]]   # by scan_ref
    footprints: dict[str, Footprint]                          # by scan_ref


@dataclass(frozen=True, slots=True)
class SceneRevision:
    """A scored scene, as a canonical payload plus its digest.

    ``payload`` is the digest-free content — everything the digest is taken over. Keeping the
    revision as one canonical dict rather than a parallel set of typed fields means there is
    exactly one marshalling of it, so ``to_dict`` cannot drift out of step with ``digest``.
    """

    payload: dict
    digest: str

    def to_dict(self) -> dict:
        return {**self.payload, "digest": self.digest}


# ----------------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------------
def build_scene(observations, *, factors=None, catalogue_version: str = "",
                margin_nats: float | None = None,
                alt_cap: int | None = None) -> SceneRevision:
    """Score ``observations`` into a ``SceneRevision``. Pure, deterministic, replayable.

    ``factors`` defaults to the live ``FACTORS`` registry; ``catalogue_version`` is a
    caller-supplied stamp, because the scene never imports the catalogue.

    Payload alignment contract: a cluster's ``lofo[name]`` is a POSITIONAL list, index-aligned
    with that cluster's ``alternatives`` — ``lofo[name][i]`` is the posterior of
    ``alternatives[i]`` with factor ``name``'s terms removed. The payload states this as
    ``lofo_alignment`` so an external consumer cannot silently read it as a mapping.
    """
    # Constants resolve AT CALL TIME, never as default-argument captures: a default evaluated
    # at import binds the module constant's value forever, so monkeypatching or an
    # owner-retuned constant would silently not apply to callers that omit the argument.
    margin = SCENE_PRUNE_MARGIN if margin_nats is None else float(margin_nats)
    cap = SCENE_ALT_CAP if alt_cap is None else int(alt_cap)
    if cap < 1:
        raise ValueError(f"alt_cap must be >= 1, got {cap}")
    factor_list = _factor_list(FACTORS if factors is None else factors)

    obs = _validated(observations)
    footprints = {o.scan_ref: footprint_of(o) for o in obs}

    scoring, non_scoring = _split_by_evidence(obs)
    components = {o.scan_ref: _components_of(o, footprints[o.scan_ref]) for o in scoring}

    clusters = _clusters(scoring, components, factor_list)
    pruning = {o.scan_ref: _prune(o, margin) for o in scoring}

    by_ref = {o.scan_ref: o for o in scoring}
    cluster_payloads = [
        _score_cluster(refs, by_ref, pruning, components, footprints, factor_list, cap)
        for refs in clusters]

    payload = {
        "schema": SCENE_SCHEMA,
        "catalogue_version": str(catalogue_version),
        "registry_fingerprint": registry_fingerprint(factor_list),
        # The values ACTUALLY used, not the module constants: a revision built with a tuned
        # margin must say so, or a replay would silently use a different model.
        "margin_nats": margin,
        "alt_cap": cap,
        # Declares how to read every cluster's lofo lists: positionally, against that
        # cluster's ``alternatives``.
        "lofo_alignment": "by_alternative_index",
        # Constraint-transport provenance: declared and dormant (the transport channel lives
        # in local evidence, per the PR-a design), so the field exists before it is filled.
        "transport": None,
        "observations": [{"scan_ref": o.scan_ref, "fingerprint": o.fingerprint,
                          "candidates": [c.to_dict() for c in o.candidates]} for o in obs],
        "non_scoring": non_scoring,
        "footprints": [footprints[o.scan_ref].to_dict() for o in obs],
        "clusters": cluster_payloads,
    }
    return SceneRevision(payload=payload, digest=fingerprint(payload))


# ----------------------------------------------------------------------------
# 1. Intake
# ----------------------------------------------------------------------------
def _validated(observations) -> tuple[SceneObservation, ...]:
    """Sort by scan_ref, with candidates in label order; refuse duplicates and blank digests.

    Sorting here is what makes the revision input-order invariant. Duplicate ``scan_ref`` is a
    caller bug — the workspace adapter's master-supersedes-members rule guarantees uniqueness,
    and quietly keeping one of two rows for the same arena would count its evidence twice or
    drop it, both silently.
    """
    seen = set()
    out = []
    for o in observations:
        if o.scan_ref in seen:
            raise ValueError(f"duplicate scan_ref {o.scan_ref!r}: one arena, one row")
        seen.add(o.scan_ref)
        if not o.fingerprint or o.fingerprint == "None":
            raise ValueError(f"observation {o.scan_ref!r} carries no fingerprint: "
                             "an unidentified arena cannot be replayed")
        labels = [c.label for c in o.candidates]
        if len(set(labels)) != len(labels):
            # The scorer keys evidence and rows on (scan_ref, label). Two rows sharing a label
            # would silently drop one row's lnZ, enumerate the survivor once per duplicate, and
            # double its posterior mass — and the cluster would still normalize to 1.0, so
            # nothing downstream could notice.
            raise ValueError(f"observation {o.scan_ref!r} has duplicate candidate labels: "
                             f"{sorted(l for l in set(labels) if labels.count(l) > 1)}")
        out.append(replace(o, candidates=tuple(sorted(o.candidates, key=lambda c: c.label))))
    return tuple(sorted(out, key=lambda o: o.scan_ref))


def _factor_list(factors) -> tuple:
    values = tuple(factors.values()) if isinstance(factors, dict) else tuple(factors)
    for f in values:
        validate_factor(f)
    names = [f.name for f in values]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate scene factor names: {sorted(names)}")
    return tuple(sorted(values, key=lambda f: f.name))


# ----------------------------------------------------------------------------
# 2. Evidence-bearing split
# ----------------------------------------------------------------------------
def _usable_lnz(c: CandidateRow) -> float | None:
    """The candidate's lnZ if it may be summed, else ``None``. ±inf is not a number to sum."""
    if not c.usable or c.log_evidence is None or not math.isfinite(c.log_evidence):
        return None
    return float(c.log_evidence)


def _split_by_evidence(obs):
    """Arenas that participate in scoring, and the experiment-map-only rest with a reason."""
    scoring, non_scoring = [], []
    for o in obs:
        if any(_usable_lnz(c) is not None for c in o.candidates):
            scoring.append(o)
        else:
            # No lnZ → no term in ln W(S). Such an arena is not "excluded"; it simply has
            # nothing to contribute, and its footprint still records that we looked.
            non_scoring.append({"scan_ref": o.scan_ref,
                                "reason": "no_candidates" if not o.candidates else "no_evidence"})
    return tuple(scoring), non_scoring


# ----------------------------------------------------------------------------
# 3. Components
# ----------------------------------------------------------------------------
def _components_of(o: SceneObservation, fp: Footprint) -> tuple[ComponentObservation, ...]:
    """One component per mode of every candidate of ``o``.

    A ``None`` ω is kept as a component with ``omega=None`` — the mode exists, it is merely
    unplaceable, and dropping it would make a candidate look like it had fewer modes.
    """
    return tuple(
        ComponentObservation(
            scan_ref=o.scan_ref,
            component_id=f"{o.scan_ref}#{c.label}#{i}",
            candidate_label=c.label,
            q=o.q,
            omega=None if w is None else float(w),
            footprint=fp,
            visibility=Visibility.OBSERVED)
        for c in o.candidates for i, w in enumerate(c.omegas))


# ----------------------------------------------------------------------------
# 4. Ambiguity graph → clusters
# ----------------------------------------------------------------------------
def _clusters(scoring, components, factor_list) -> list[tuple[str, ...]]:
    """Connected components of the ambiguity graph, recomputed from scratch every call.

    With no coupling factor every arena is its own cluster — the correct degenerate case: the
    scene then reduces to independent per-arena posteriors, which is exactly what independent
    scans mean.
    """
    refs = [o.scan_ref for o in scoring]
    parent = {r: r for r in refs}

    def find(r):
        while parent[r] != r:
            parent[r] = parent[parent[r]]
            r = parent[r]
        return r

    couplers = [f.couples for f in factor_list if f.couples is not None]
    if couplers:
        # ponytail: O(arenas² × components²) all-pairs probe. Fine at campaign scale (tens of
        # arenas); a q-bucketed index is the upgrade path if a scene ever holds thousands.
        for a, b in itertools.combinations(refs, 2):
            if find(a) == find(b):
                continue
            if any(couples(ca, cb)
                   for couples in couplers
                   for ca in components[a] for cb in components[b]):
                parent[find(a)] = find(b)

    groups: dict[str, list[str]] = {}
    for r in refs:
        groups.setdefault(find(r), []).append(r)
    return sorted((tuple(sorted(v)) for v in groups.values()), key=lambda t: t[0])


# ----------------------------------------------------------------------------
# 5. Per-arena margin pruning
# ----------------------------------------------------------------------------
def _prune(o: SceneObservation, margin: float) -> dict:
    """Split one arena's candidates into survivors and recorded exclusions."""
    leader = max(z for z in (_usable_lnz(c) for c in o.candidates) if z is not None)
    survivors, excluded = [], []
    for c in o.candidates:
        lnz = _usable_lnz(c)
        if lnz is None:
            # Never had a bound to state: an unusable or non-finite candidate is not a
            # candidate that is merely improbable.
            excluded.append({"label": c.label, "reason": "no_evidence",
                             "delta_nats": None, "mass_bound": None})
        elif lnz < leader - margin:
            delta = leader - lnz
            excluded.append({"label": c.label, "reason": "margin_pruned",
                             "delta_nats": delta,
                             # Upper bound on the posterior mass dropped relative to the
                             # arena leader. Recorded once, here, so no later reader has to
                             # re-derive it from a margin that may since have been retuned.
                             "mass_bound": math.exp(-delta)})
        else:
            survivors.append((c, lnz))
    return {"survivors": survivors, "excluded": excluded, "leader": leader}


# ----------------------------------------------------------------------------
# 6. Alternative enumeration
# ----------------------------------------------------------------------------
def _enumerate(arenas, cap: int):
    """``(assignments, n_possible, truncated, discarded_mass_bound)`` for one cluster.

    ``arenas`` is ``[(scan_ref, [(label, lnZ), ...]), ...]`` in scan_ref order. Below the cap
    the full Cartesian product is enumerated. Above it the kept set is EXACTLY the top-``cap``
    alternatives by base Σ lnZ (ties by label tuple), found by a best-first walk over the
    product — an approximation here would put an unstated bias in the posterior, whereas a
    truncation with a stated mass bound is honest.
    """
    n_possible = math.prod(len(cands) for _, cands in arenas)
    if n_possible <= cap:
        assignments = [tuple((ref, cands[i][0]) for (ref, cands), i in zip(arenas, idx))
                       for idx in itertools.product(*(range(len(c)) for _, c in arenas))]
        return sorted(assignments), n_possible, False, 0.0

    # Best-first: each arena's candidates sorted by lnZ descending, so incrementing any index
    # can only lower the base score — the frontier heap therefore pops in exact base order.
    ranked = [(ref, sorted(cands, key=lambda t: (-t[1], t[0]))) for ref, cands in arenas]

    def base_of(idx):
        return math.fsum(cands[i][1] for (_, cands), i in zip(ranked, idx))

    def labels_of(idx):
        return tuple((ref, cands[i][0]) for (ref, cands), i in zip(ranked, idx))

    start = (0,) * len(ranked)
    heap = [(-base_of(start), labels_of(start), start)]
    seen = {start}
    kept, leader_base, lowest_base = [], None, None
    while heap and len(kept) < cap:
        neg_base, labels, idx = heapq.heappop(heap)
        if leader_base is None:
            leader_base = -neg_base
        lowest_base = -neg_base
        kept.append(labels)
        for pos in range(len(ranked)):
            nxt = idx[:pos] + (idx[pos] + 1,) + idx[pos + 1:]
            if nxt[pos] < len(ranked[pos][1]) and nxt not in seen:
                seen.add(nxt)
                heapq.heappush(heap, (-base_of(nxt), labels_of(nxt), nxt))
    discarded = _discarded_bound(n_possible - len(kept), lowest_base, leader_base)
    return sorted(tuple(sorted(a)) for a in kept), n_possible, True, discarded


def _discarded_bound(n_dropped: int, lowest_base: float, leader_base: float) -> float:
    """Upper bound on the mass a cap dropped: ``n_dropped × exp(lowest − leader)``.

    Conservative and stated: every dropped alternative has base ≤ the lowest kept base, so its
    unnormalized weight relative to the leader is at most ``exp(lowest − leader)``.

    Computed in log space because ``n_dropped`` is an exact Python int over the full product
    and can exceed the float range long before the bound itself does — ``math.log`` takes the
    big int exactly, where multiplying would raise ``OverflowError`` on the conversion. A bound
    that genuinely overflows is recorded as ``inf``: an infinite stated bound is honest (it
    says the cap dropped an unbounded share), a crash in the scorer is not.
    """
    if n_dropped <= 0:
        return 0.0
    try:
        return math.exp(math.log(n_dropped) + (lowest_base - leader_base))
    except OverflowError:
        return math.inf


# ----------------------------------------------------------------------------
# 7-9. Scoring, ledger, marginals
# ----------------------------------------------------------------------------
def _score_cluster(refs, by_ref, pruning, components, footprints, factor_list, cap) -> dict:
    cluster_id = refs[0]
    arenas = [(r, [(c.label, lnz) for c, lnz in pruning[r]["survivors"]]) for r in refs]
    assignments, n_possible, truncated, discarded = _enumerate(arenas, cap)

    view = ClusterView(cluster_id=cluster_id, scan_refs=refs,
                       components={r: components[r] for r in refs},
                       footprints={r: footprints[r] for r in refs})
    lnz_of = {(r, c.label): lnz for r in refs for c, lnz in pruning[r]["survivors"]}
    rows_of = {(r, c.label): c for r in refs for c in by_ref[r].candidates}

    entries = []
    for assignment in assignments:
        chosen = {ref: label for ref, label in assignment}
        # ONE lnZ per arena. A chosen candidate with n_modes=2 contributes its evidence once;
        # its modes are components below, and components carry no evidence at all.
        base = math.fsum(lnz_of[(ref, label)] for ref, label in assignment)
        alt = SceneAlternative(
            cluster_id=cluster_id, assignment=assignment,
            components=tuple(c for ref in refs for c in components[ref]
                             if c.candidate_label == chosen[ref]))
        factor_terms, prior_terms, reasons = {}, {}, {}
        for f in factor_list:
            if f.score is None:
                continue
            term, why = f.score(alt, view)
            (prior_terms if f.kind == "scene_prior" else factor_terms)[f.name] = float(term)
            if why:
                reasons[f.name] = list(why)
        entries.append({
            "assignment": [list(pair) for pair in assignment],
            "base": base,
            "factor_terms": factor_terms,
            "prior_terms": prior_terms,
            "reasons": reasons,
            # Summed in sorted-name order over both term maps, so a reader recomputing
            # total = fsum([base, *terms-by-sorted-name]) reproduces this float exactly.
            "total": _total(base, factor_terms, prior_terms),
        })

    log_norm = _logsumexp([e["total"] for e in entries])
    for e in entries:
        e["posterior"] = math.exp(e["total"] - log_norm)

    lofo = {}
    for f in factor_list:
        if f.score is None:
            continue
        # Leave-one-factor-out over the FIXED alternative set, index-aligned with ``entries``
        # (payload ``lofo_alignment``): the question is what that
        # factor's terms did to the posterior, not what a differently-enumerated scene would
        # have looked like, so re-enumeration would answer a different question.
        totals = [_total(e["base"], _without(e["factor_terms"], f.name),
                         _without(e["prior_terms"], f.name)) for e in entries]
        norm = _logsumexp(totals)
        lofo[f.name] = [math.exp(t - norm) for t in totals]

    return {
        "cluster_id": cluster_id,
        "scan_refs": list(refs),
        "truncated": truncated,
        "n_enumerated": len(entries),
        "n_possible": n_possible,
        "discarded_mass_bound": discarded,
        "pruning": {r: pruning[r]["excluded"] for r in refs},
        "log_norm": log_norm,
        "alternatives": entries,
        "lofo": lofo,
        "marginals": _marginals(refs, entries, rows_of, truncated),
    }


def _without(terms: dict, name: str) -> dict:
    return {k: v for k, v in terms.items() if k != name}


def _total(base: float, factor_terms: dict, prior_terms: dict) -> float:
    merged = {**factor_terms, **prior_terms}
    return math.fsum([base, *(merged[k] for k in sorted(merged))])


def _logsumexp(values) -> float:
    """Pure-python max-shifted log-sum-exp — no numpy in the scene layer."""
    top = max(values)
    if top == -math.inf:
        return top
    return top + math.log(math.fsum(math.exp(v - top) for v in values))


def _marginals(refs, entries, rows_of, truncated: bool) -> dict:
    """Exact sums over the enumerated alternative set: per arena, P(label) and P(n_modes=k).

    The mode-count marginal is the N-mode ruling's representable uncertainty (H8(e)):
    "three modes, poorly constrained" has to survive as a distribution, not collapse to a
    winner. ``truncated`` travels with the numbers so a reader never mistakes a capped sum
    for a complete one.
    """
    labels: dict[str, dict[str, float]] = {r: {} for r in refs}
    counts: dict[str, dict[str, float]] = {r: {} for r in refs}
    for e in entries:
        for ref, label in e["assignment"]:
            labels[ref][label] = labels[ref].get(label, 0.0) + e["posterior"]
            k = str(rows_of[(ref, label)].n_modes)
            counts[ref][k] = counts[ref].get(k, 0.0) + e["posterior"]
    return {"label": labels, "n_modes": counts, "truncated": truncated}
