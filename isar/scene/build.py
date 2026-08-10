"""Observations → a cluster-factorized, exactly-scored scene revision (SCENE-3 PR 3a).

This is the arithmetic of SCENE_DESIGN §2b and nothing else. For a cluster of observation
arenas, an alternative ``S`` picks exactly one surviving candidate per arena AND one linkage
reading of those candidates' components (the connectivity variable, SCENE-3 PR 3b), and

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
  unrelated arena cannot move an existing cluster's posteriors by a single bit. Only SURVIVING
  candidates build the graph: a candidate nobody will score may not merge two clusters into a
  joint normalization it takes no part in.
* **Nothing is discarded on base lnZ that a factor could rescue.** Margin pruning and the
  enumeration cap both decide on ``Σ lnZ`` before any factor term exists, so both are permitted
  only within the factor set's DECLARED score span, and both record bounds widened by it. One
  unbounded scoring factor disables margin pruning outright and makes a truncated cluster state
  no bound at all — an unresolved cluster is honest, a false bound is not.
* **Byte-identical replay.** The revision carries no timestamp and no machine identity; every
  ordering is derived from the data (scan_ref, then label), so two builds of the same inputs
  produce the same digest.

Stdlib-only and pure: no filesystem, no ``isar.session``/``analyze``/``drive``. The one
exception is ``isar.schema.steering``'s evidence-sink vocabulary — itself stdlib-only, and the
single definition of what routing a scan's evidence was acquired for; a local copy of those
strings would silently diverge from the schema the writer stamps.
"""
from __future__ import annotations

import heapq
import itertools
import json
import math
from dataclasses import dataclass, replace
from types import MappingProxyType

from isar.schema.steering import CANONICAL_MASTER, EVIDENCE_SINKS

from .factors import (FACTORS, factor_manifest, neighbour_reports, registry_fingerprint,
                      validate_factor)
from .model import (CandidateRow, ComponentObservation, Footprint, SceneObservation,
                    Visibility, canonical_bytes, digest_bytes, footprint_of, is_mode, link_tol,
                    q_adjacent)

SCENE_SCHEMA = "isar.scene-revision/1"

# Same currency and idiom as isar/analyze/evidence.py PRUNE_MARGIN: nats below the cluster's
# per-arena leader, beyond which a candidate's posterior mass is bounded by e^−margin and it
# stops being a contender. Owner-tunable. NOTE this is the BARE margin: under active scoring
# factors the builder widens it by the declared factor span (``_score_span``), because a factor
# term the pruner has not seen yet can rescue a candidate the bare margin would discard.
SCENE_PRUNE_MARGIN = 100.0

# Hard cap on alternatives enumerated per cluster. Owner-tunable; a truncated cluster says so
# and carries a stated bound on the mass it dropped — or, when a factor declares no bound, says
# it can state no bound at all.
#
# 4096 makes EXACT enumeration the ordinary posture rather than the lucky one: a realistic
# cluster (six arenas × three candidates = 729) sat above the old 64 and was truncated by
# default, so every such scene published a capped posterior with a bound instead of the answer.
SCENE_ALT_CAP = 4096

# Hard cap on the LINKAGE variants kept per candidate assignment. Owner-tunable. Linkage variants
# multiply the assignment count, and a cluster with many ambiguous junctions would otherwise
# enumerate 2^junctions readings of one assignment. Truncation is recorded per cluster
# (``linkage_truncated``) with the same honesty as the alternative cap: linkage variants share a
# base Σ lnZ exactly, so no bound on the dropped mass can be stated for them either.
MAX_LINKAGE_VARIANTS = 8


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
    # The scene's connectivity variable (SCENE_DESIGN §2b: "connectivity/topology states"): which
    # component at one arena is the SAME TRACK as which component at an adjacent one, as sorted
    # ``(component_id_a, component_id_b)`` pairs. Two alternatives may share an assignment and
    # differ only here — that is the branch-crossing ambiguity, and since they share the same local
    # fits their base Σ lnZ is identical, so only factor terms can separate them.
    links: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ClusterView:
    """The cluster context handed to ``score`` alongside the alternative.

    ``components`` holds every arena's SURVIVING components (not only the chosen ones) so a
    factor can reason about what *could* have been there; ``footprints`` is the experiment map,
    which is how a factor prices a coverage gap without inventing an update for it.

    Both mappings are read-only views. A frozen dataclass freezes the *fields*, not the dicts
    behind them: a factor that wrote into ``view.components`` would mutate the state every later
    factor and every later alternative of the same cluster then scores against, making the
    revision depend on factor order and on nothing recorded anywhere.
    """

    cluster_id: str
    scan_refs: tuple[str, ...]
    components: MappingProxyType   # scan_ref -> tuple[ComponentObservation, ...]
    footprints: MappingProxyType   # scan_ref -> Footprint


@dataclass(frozen=True, slots=True)
class SceneRevision:
    """A scored scene, as its canonical bytes plus their digest.

    The bytes are the revision. They are encoded ONCE, at construction, and digested from the
    same object that is stored — so ``payload`` can hand out a fresh dict per call without any
    risk that a caller's edit to that dict desynchronizes the content from ``digest``. Keeping
    the revision as one canonical encoding rather than a parallel set of typed fields means
    there is exactly one marshalling of it, so ``to_dict`` cannot drift out of step either.
    """

    canonical: bytes
    digest: str

    @classmethod
    def of(cls, payload: dict) -> "SceneRevision":
        blob = canonical_bytes(payload)
        return cls(canonical=blob, digest=digest_bytes(blob))

    @property
    def payload(self) -> dict:
        """A FRESH copy of the digested content, every call. Mutating it changes nothing."""
        return json.loads(self.canonical)

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

    Payload alignment contract: a cluster's ``score_ablation[name]`` is a POSITIONAL list,
    index-aligned with that cluster's ``alternatives`` — ``score_ablation[name][i]`` is the
    posterior of ``alternatives[i]`` with factor ``name``'s score terms removed. The payload
    states this as ``score_ablation_alignment`` so an external consumer cannot silently read it
    as a mapping.
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
    # Order matters and is the contract: evidence split → margin pruning → components of the
    # SURVIVORS → coupling edges → clusters → enumeration. A candidate that will not be
    # enumerated must not shape the partition either: an excluded candidate's component could
    # otherwise couple two arenas and merge two clusters, and since truncation and normalization
    # are joint over a cluster, a candidate nobody scores would still move everyone's posterior.
    span = _score_span(factor_list)
    pruning = {o.scan_ref: _prune(o, margin, span) for o in scoring}
    components = {o.scan_ref: _components_of(o, footprints[o.scan_ref],
                                             _survivor_labels(pruning[o.scan_ref]))
                  for o in scoring}

    clusters = _clusters(scoring, components, factor_list)

    by_ref = {o.scan_ref: o for o in scoring}
    cluster_payloads = [
        _score_cluster(refs, by_ref, pruning, components, footprints, factor_list, cap, span)
        for refs in clusters]

    payload = {
        "schema": SCENE_SCHEMA,
        "catalogue_version": str(catalogue_version),
        "registry_fingerprint": registry_fingerprint(factor_list),
        # The declarations that fingerprint stands for, in full, so a reader can see WHAT scored
        # the scene rather than only that something did.
        "factor_manifest": factor_manifest(factor_list),
        # The values ACTUALLY used, not the module constants: a revision built with a tuned
        # margin must say so, or a replay would silently use a different model.
        "margin_nats": margin,
        "alt_cap": cap,
        # Σ_f (hi_f − lo_f) over the active scoring factors — the nats of rescue the pruner had
        # to allow for. ``None`` = at least one factor declared no bound, so no base-lnZ pruning
        # or truncation bound was permitted at all.
        "score_span_nats": span,
        # Declares how to read every cluster's score_ablation lists: positionally, against that
        # cluster's ``alternatives``.
        "score_ablation_alignment": "by_alternative_index",
        # Constraint-transport provenance: declared and dormant (the transport channel lives
        # in local evidence, per the PR-a design), so the field exists before it is filled.
        "transport": None,
        "observations": [{"scan_ref": o.scan_ref, "fingerprint": o.fingerprint,
                          "candidates": [c.to_dict() for c in o.candidates]} for o in obs],
        "non_scoring": non_scoring,
        "footprints": [footprints[o.scan_ref].to_dict() for o in obs],
        "clusters": cluster_payloads,
    }
    return SceneRevision.of(payload)


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


def _score_span(factor_list) -> float | None:
    """``Σ_f (hi_f − lo_f)`` over the ACTIVE SCORING factors, or ``None`` if any is unbounded.

    This is the total nats by which factor terms can reorder two alternatives that the base
    Σ lnZ has already separated. Everything the builder discards without enumerating it — a
    margin-pruned candidate, a capped alternative — is discarded on base alone, so this span is
    exactly the slack those decisions must carry, and its absence is exactly the reason to stop
    discarding. A coupling-only factor is irrelevant here: it changes the partition, not the
    score, and cannot rescue anything.
    """
    scorers = [f for f in factor_list if f.score is not None]
    if any(f.score_bound is None for f in scorers):
        return None
    return math.fsum(hi - lo for lo, hi in (f.score_bound for f in scorers))


# ----------------------------------------------------------------------------
# 2. Evidence-bearing split
# ----------------------------------------------------------------------------
def _usable_lnz(c: CandidateRow) -> float | None:
    """The candidate's lnZ if it may be summed, else ``None``. ±inf is not a number to sum."""
    if not c.usable or c.log_evidence is None or not math.isfinite(c.log_evidence):
        return None
    return float(c.log_evidence)


def _non_scoring_reason(o: SceneObservation) -> str | None:
    """Why this arena may not enter ``ln W(S)``, or ``None`` if it may.

    Fail-closed, and in this order: routing first (what this evidence IS), then whether the
    comparison it came from was finished, then whether there is a summable lnZ at all. A
    non-scoring arena keeps its footprint and its candidate rows either way — nothing is
    deleted, the information is retained and only its use is declined.
    """
    sink = o.evidence_sink
    if sink is not None and sink not in EVIDENCE_SINKS:
        # Includes the reader's own UNKNOWN_SINK marker. A routing this repo cannot interpret is
        # quarantined, never read as ordinary evidence — ``canonical_master`` is the most
        # privileged sink there is, not a safe default.
        return "unknown_sink"
    if sink is not None and sink != CANONICAL_MASTER:
        # An auxiliary sink is a scientific evidence channel of its own (identity, connectivity,
        # transition), not lesser evidence — but it answers a different question than the
        # canonical pool, and the scene routes it in a later slice. Deferred, not discarded.
        return "aux_sink_deferred"
    if not o.candidates:
        return "no_candidates"
    if o.evidence_comparison_status == "incomplete":
        # H7: the candidate comparison expired with a contender unresolved, so the lnZ values
        # present are not the arena. Scoring them would turn an unfinished comparison into a
        # confident posterior — the one failure this status exists to prevent.
        return "evidence_incomplete"
    if o.evidence_comparison_status != "complete":
        return "no_evidence"
    if not any(_usable_lnz(c) is not None for c in o.candidates):
        # No lnZ → no term in ln W(S). Such an arena is not "excluded"; it simply has nothing to
        # contribute, and its footprint still records that we looked.
        return "no_evidence"
    return None


def _split_by_evidence(obs):
    """Arenas that participate in scoring, and the experiment-map-only rest with a reason."""
    scoring, non_scoring = [], []
    for o in obs:
        reason = _non_scoring_reason(o)
        if reason is None:
            scoring.append(o)
        else:
            non_scoring.append({"scan_ref": o.scan_ref, "reason": reason})
    return tuple(scoring), non_scoring


# ----------------------------------------------------------------------------
# 3. Components
# ----------------------------------------------------------------------------
def _structure_terms(label: str) -> tuple[str, ...]:
    """The component terms of a candidate label, per the catalogue's label grammar.

    Grammar, read off the code that builds it rather than guessed:
    ``isar/analyze/catalogue.py:enumerate_structures`` / ``enumerate_multimode_structures`` join
    the term names with ``+`` (``elastic+stokes+qe+bg``, ``stokes2+bg``);
    ``_phonon_bearing_adder`` appends ``@<identity>`` to the WHOLE label; and
    ``isar/analyze/loop.py:1030`` appends a variant suffix (``_neardeg``) to the whole label
    after that. Both suffixes therefore land on the LAST term, and ``elastic``/``qe`` are never
    last (``bg`` always is), so splitting the pre-``@`` part on ``+`` identifies them exactly.
    """
    return tuple(label.split("@", 1)[0].split("+"))


def _components_of(o: SceneObservation, fp: Footprint,
                   labels: frozenset) -> tuple[ComponentObservation, ...]:
    """One component per mode of every SURVIVING candidate of ``o``, plus its non-mode components.

    ``labels`` is the surviving set from ``_prune``. A discarded candidate emits no component,
    so it can neither be coupled on nor scored — a candidate excluded for having no evidence,
    or pruned as hopeless, must not be able to bridge two otherwise-independent clusters into
    one joint normalization it takes no part in.

    A ``None`` ω is kept as a component with ``omega=None`` — the mode exists, it is merely
    unplaceable, and dropping it would make a candidate look like it had fewer modes.

    A structure's ELASTIC and QUASIELASTIC terms are components too, at ω=0: without them an
    ``elastic_only`` arena would emit nothing and could never couple to its neighbours, so the
    scene could say nothing about a run of phonon-less arenas — which is a physical claim
    ("nothing inelastic here, over this stretch of q"), not an absence of data. A bg-only
    candidate still emits nothing: there really is nothing there.
    """
    return tuple(
        ComponentObservation(
            scan_ref=o.scan_ref,
            component_id=f"{o.scan_ref}#{c.label}#{tag}",
            candidate_label=c.label,
            q=o.q,
            omega=omega,
            footprint=fp,
            visibility=Visibility.OBSERVED)
        for c in o.candidates if c.label in labels
        for tag, omega in _component_slots(c))


def _component_slots(c: CandidateRow):
    """``(id tag, ω)`` for one candidate: its modes by index, then its non-mode terms."""
    for i, w in enumerate(c.omegas):
        yield str(i), (None if w is None else float(w))
    terms = _structure_terms(c.label)
    for term in ("elastic", "qe"):
        if term in terms:
            yield term, 0.0


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
def _prune(o: SceneObservation, margin: float, span: float | None) -> dict:
    """Split one arena's candidates into survivors and recorded exclusions.

    Margin pruning is a statement about the FULL score, not about lnZ, so it is only legal
    within the declared factor span. Take an alternative S using candidate ``c``, and S′ the
    same alternative with ``c`` swapped for the arena leader. Then

        ln W(S) − ln W(S′) = (lnZ(c) − lnZ(leader)) + Σ_f (φ_f(S) − φ_f(S′))
                           ≤ −delta + Σ_f (hi_f − lo_f) = −delta + span,

    so ``c``'s weight relative to that swapped rival is bounded by ``exp(span − delta)``: the
    candidate is only safe to discard once ``delta > margin + span``, and the honest bound to
    record is the widened one. With ``span = 0`` (no scoring factors, or all neutral) both
    collapse to the bare margin and ``exp(−delta)`` — the empty-registry behaviour, unchanged.

    ``span is None`` means some scoring factor declared no bound: an arbitrarily large term
    could rescue any candidate, so NO margin pruning happens at all. Only the exclusions a
    factor cannot reverse remain — a candidate with no summable lnZ has no term to be rescued.
    """
    leader = max(z for z in (_usable_lnz(c) for c in o.candidates) if z is not None)
    effective = None if span is None else margin + span
    survivors, excluded = [], []
    for c in o.candidates:
        lnz = _usable_lnz(c)
        if lnz is None:
            # Never had a bound to state: an unusable or non-finite candidate is not a
            # candidate that is merely improbable.
            excluded.append({"label": c.label, "reason": "no_evidence", "delta_nats": None,
                             "mass_bound": None, "effective_margin_nats": None})
        elif effective is not None and lnz < leader - effective:
            delta = leader - lnz
            excluded.append({"label": c.label, "reason": "margin_pruned",
                             "delta_nats": delta,
                             # Upper bound on the posterior mass dropped relative to the
                             # arena leader, factor rescue included. Recorded once, here, so no
                             # later reader has to re-derive it from a margin or a factor set
                             # that may since have been retuned.
                             "mass_bound": math.exp(span - delta),
                             # The margin actually applied, which is not the payload's
                             # ``margin_nats`` whenever a scoring factor is active.
                             "effective_margin_nats": effective})
        else:
            survivors.append((c, lnz))
    return {"survivors": survivors, "excluded": excluded, "leader": leader}


def _survivor_labels(pruned: dict) -> frozenset:
    return frozenset(c.label for c, _ in pruned["survivors"])


# ----------------------------------------------------------------------------
# 6. Alternative enumeration
# ----------------------------------------------------------------------------
def _enumerate(arenas, cap: int, span: float | None):
    """``(assignments, n_possible, truncated, discarded_mass_bound, unbounded)`` for a cluster.

    ``arenas`` is ``[(scan_ref, [(label, lnZ), ...]), ...]`` in scan_ref order. Below the cap
    the full Cartesian product is enumerated. Above it the kept set is EXACTLY the top-``cap``
    alternatives by base Σ lnZ (ties by label tuple), found by a best-first walk over the
    product — an approximation here would put an unstated bias in the posterior, whereas a
    truncation with a stated mass bound is honest.

    Why the base ordering IS the optimistic ordering. A factor's declared bound is a bound on
    its total term for *any* alternative, so the optimistic score ``base(S) + Σ_f hi_f`` differs
    from ``base(S)`` by a constant across the whole cluster; ranking by it therefore selects the
    same set in the same order. There is no bounded re-ranking to be had, and the consequence is
    stated rather than hidden: a cap under active factors keeps the top of the BASE ordering,
    which is not necessarily the top of the total ordering, and the widened bound below is what
    makes the payload honest about it. (A bound ≥ 1 is a true bound that states it constrains
    nothing — which is the point at which the operator should raise ``alt_cap``.)
    """
    n_possible = math.prod(len(cands) for _, cands in arenas)
    if n_possible <= cap:
        assignments = [tuple((ref, cands[i][0]) for (ref, cands), i in zip(arenas, idx))
                       for idx in itertools.product(*(range(len(c)) for _, c in arenas))]
        return sorted(assignments), n_possible, False, 0.0, False

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
    if span is None:
        # World-11 honesty: an unbounded factor can lift any dropped alternative arbitrarily far,
        # so there is NO number here that is both true and useful. A truncated cluster with no
        # bound is an unresolved cluster, and says so — inventing exp(lowest − leader) would be
        # the reviewer's failure exactly: a payload claiming ≤0.0067 of dropped mass where the
        # truth is 0.993.
        return sorted(tuple(sorted(a)) for a in kept), n_possible, True, None, True
    discarded = _discarded_bound(n_possible - len(kept), lowest_base, leader_base, span)
    return sorted(tuple(sorted(a)) for a in kept), n_possible, True, discarded, False


def _discarded_bound(n_dropped: int, lowest_base: float, leader_base: float,
                     span: float = 0.0) -> float:
    """Upper bound on the mass a cap dropped: ``n_dropped × exp(lowest − leader + span)``.

    Conservative and stated. Every dropped alternative D has ``base(D) ≤ lowest`` (the kept set
    is the top of the base ordering) and, for the best kept alternative L with ``base(L) =
    leader``,

        ln W(D) − ln W(L) = base(D) − base(L) + Σ_f (φ_f(D) − φ_f(L))
                          ≤ (lowest − leader) + Σ_f (hi_f − lo_f) = lowest − leader + span,

    so each dropped alternative's unnormalized weight relative to the leader is at most
    ``exp(lowest − leader + span)``, and there are ``n_dropped`` of them. ``span = 0`` is the
    no-scoring-factor case and reproduces the bare ``n_dropped × exp(lowest − leader)``.

    Computed in log space because ``n_dropped`` is an exact Python int over the full product
    and can exceed the float range long before the bound itself does — ``math.log`` takes the
    big int exactly, where multiplying would raise ``OverflowError`` on the conversion. A bound
    that genuinely overflows is recorded as ``inf``: an infinite stated bound is honest (it
    says the cap dropped an unbounded share), a crash in the scorer is not.
    """
    if n_dropped <= 0:
        return 0.0
    try:
        return math.exp(math.log(n_dropped) + (lowest_base - leader_base) + span)
    except OverflowError:
        return math.inf


# ----------------------------------------------------------------------------
# 6b. Linkage variants (the connectivity variable)
# ----------------------------------------------------------------------------
def _default_links(alt: SceneAlternative, view: ClusterView) -> tuple[tuple[str, str], ...]:
    """Nearest-ω chaining between adjacent arenas: the linkage assumed unless a factor proposes.

    Deterministic and greedy: for each adjacent arena pair in scan_ref order, each mode of the
    lower arena takes the closest unclaimed mode of the higher one within ``link_tol`` (ties by
    component id). A mode with no partner in tolerance stays UNLINKED rather than being forced
    onto a distant one — a forced link would invent a track the data does not support, and an
    unlinked mode is exactly what the connectivity factor prices as a possible break.
    """
    chosen: dict[str, list] = {}
    for c in alt.components:
        chosen.setdefault(c.scan_ref, []).append(c)
    links = []
    for a in range(len(view.scan_refs)):
        for b in range(a + 1, len(view.scan_refs)):
            i, j = view.scan_refs[a], view.scan_refs[b]
            fp_i, fp_j = view.footprints[i], view.footprints[j]
            if not q_adjacent(fp_i.q, fp_j.q):
                continue
            tol = link_tol(fp_i, fp_j)
            partners = _placeable_modes(chosen.get(j, ()))
            taken = set()
            for m in _placeable_modes(chosen.get(i, ())):
                reach = [(abs(abs(p.omega) - abs(m.omega)), p.component_id) for p in partners
                         if p.component_id not in taken
                         and abs(abs(p.omega) - abs(m.omega)) <= tol]
                if not reach:
                    continue
                _, partner_id = min(reach)
                taken.add(partner_id)
                links.append((m.component_id, partner_id))
    return tuple(sorted(links))


def _placeable_modes(components) -> list:
    return sorted((c for c in components if is_mode(c) and c.omega is not None),
                  key=lambda c: (abs(c.omega), c.component_id))


def _linkage_variants(alt: SceneAlternative, view: ClusterView, factor_list):
    """``(variants, truncated)`` — the linkage readings of ONE candidate assignment.

    The default chaining always comes first, so a truncation can never drop it. Proposals come
    from the factors' ``propose`` hooks (the crossing proposer), are deduplicated, and are ordered
    deterministically before the cap applies.
    """
    default = _default_links(alt, view)
    proposed = set()
    for f in factor_list:
        if f.propose is None:
            continue
        for links in f.propose(replace(alt, links=default), view):
            variant = tuple(sorted(tuple(pair) for pair in links))
            if variant != default:
                proposed.add(variant)
    ordered = [default, *sorted(proposed)]
    return tuple(ordered[:MAX_LINKAGE_VARIANTS]), len(ordered) > MAX_LINKAGE_VARIANTS


# ----------------------------------------------------------------------------
# 7-9. Scoring, ledger, marginals
# ----------------------------------------------------------------------------
def _score_cluster(refs, by_ref, pruning, components, footprints, factor_list, cap,
                   span) -> dict:
    """One cluster's ledger. Alternatives = candidate assignments × their linkage readings.

    The cap and its bound are about ASSIGNMENTS (``n_possible``, ``n_assignments``); linkage
    variants expand each surviving assignment afterwards, so the heap enumeration never sees them
    and cannot collapse two readings of one assignment into a single kept entry.
    """
    cluster_id = refs[0]
    arenas = [(r, [(c.label, lnz) for c, lnz in pruning[r]["survivors"]]) for r in refs]
    assignments, n_possible, truncated, discarded, unbounded = _enumerate(arenas, cap, span)

    view = ClusterView(cluster_id=cluster_id, scan_refs=refs,
                       components=MappingProxyType({r: components[r] for r in refs}),
                       footprints=MappingProxyType({r: footprints[r] for r in refs}))
    lnz_of = {(r, c.label): lnz for r in refs for c, lnz in pruning[r]["survivors"]}
    rows_of = {(r, c.label): c for r in refs for c in by_ref[r].candidates}

    entries = []
    linkage_truncated = False
    for assignment in assignments:
        chosen = {ref: label for ref, label in assignment}
        # ONE lnZ per arena. A chosen candidate with n_modes=2 contributes its evidence once;
        # its modes are components below, and components carry no evidence at all.
        base = math.fsum(lnz_of[(ref, label)] for ref, label in assignment)
        assigned = SceneAlternative(
            cluster_id=cluster_id, assignment=assignment,
            components=tuple(c for ref in refs for c in components[ref]
                             if c.candidate_label == chosen[ref]))
        variants, cut = _linkage_variants(assigned, view, factor_list)
        linkage_truncated = linkage_truncated or cut
        for links in variants:
            entries.append(_score_alternative(replace(assigned, links=links), view, base,
                                              factor_list, cluster_id))

    log_norm = _logsumexp([e["total"] for e in entries])
    for e in entries:
        e["posterior"] = math.exp(e["total"] - log_norm)

    score_ablation = {}
    for f in factor_list:
        if f.score is None:
            continue
        # SCORE-TERM ablation over the FIXED alternative set, index-aligned with ``entries``
        # (payload ``score_ablation_alignment``): the question is what that factor's SCORE terms
        # did to the posterior over these hypotheses. It is deliberately not a rebuild without
        # the factor: the factor's ``couples`` edges (which alternatives exist at all, how the
        # cluster is partitioned) and its ``propose`` contributions are NOT removed, so the
        # comparison is like-for-like. Re-enumerating would answer a different question and the
        # honest name says so.
        totals = [_total(e["base"], _without(e["factor_terms"], f.name),
                         _without(e["prior_terms"], f.name)) for e in entries]
        norm = _logsumexp(totals)
        score_ablation[f.name] = [math.exp(t - norm) for t in totals]

    return {
        "cluster_id": cluster_id,
        "scan_refs": list(refs),
        "truncated": truncated,
        "n_enumerated": len(entries),
        "n_assignments": len(assignments),
        "n_possible": n_possible,
        "discarded_mass_bound": discarded,
        # True when the cap dropped alternatives under a factor that declared no bound: the
        # cluster is UNRESOLVED, not narrowed, and no reader may treat these posteriors as a
        # bounded approximation of the full ones.
        "truncation_unbounded": unbounded,
        # True when some assignment had more linkage readings than MAX_LINKAGE_VARIANTS. Linkage
        # variants share a base Σ lnZ exactly, so there is no base ordering to bound them by and
        # no honest mass bound to state — only the fact that readings were dropped.
        "linkage_truncated": linkage_truncated,
        "pruning": {r: pruning[r]["excluded"] for r in refs},
        "log_norm": log_norm,
        "alternatives": entries,
        # Why each chosen mode's absence at a neighbour was, or was not, priced — for the leading
        # alternative only, which is what a reader is looking at. Additive: a consumer that does
        # not know the key is unaffected.
        "visibility": (list(neighbour_reports(_leader(entries, refs, components, cluster_id), view))
                       if entries else []),
        "score_ablation": score_ablation,
        "marginals": _marginals(refs, entries, rows_of, truncated, unbounded),
    }


def _leader(entries, refs, components, cluster_id) -> SceneAlternative:
    """The highest-posterior alternative, rebuilt as a ``SceneAlternative``. Ties by position."""
    best = max(entries, key=lambda e: e["posterior"])
    assignment = tuple(tuple(pair) for pair in best["assignment"])
    chosen = {ref: label for ref, label in assignment}
    return SceneAlternative(
        cluster_id=cluster_id, assignment=assignment,
        components=tuple(c for ref in refs for c in components[ref]
                         if c.candidate_label == chosen[ref]),
        links=tuple(tuple(pair) for pair in best["links"]))


def _score_alternative(alt: SceneAlternative, view: ClusterView, base: float, factor_list,
                       cluster_id: str) -> dict:
    """One ledger entry: the factor terms of ``alt`` and the total they add up to."""
    assignment = alt.assignment
    factor_terms, prior_terms, reasons = {}, {}, {}
    for f in factor_list:
        if f.score is None:
            continue
        term, why = f.score(alt, view)
        term = float(term)
        if not math.isfinite(term):
            # A NaN term poisons the log-sum-exp into an all-NaN posterior that still
            # "normalizes"; a −inf term silently deletes an alternative from the cluster
            # without a pruning record. Both are caught at the source, naming it.
            raise ValueError(
                f"scene factor {f.name!r} returned a non-finite term {term!r} for cluster "
                f"{cluster_id!r}, alternative {list(assignment)}. A veto by −inf is NOT a "
                "supported semantic in v1: a factor that must forbid an alternative needs a "
                "declared veto semantic, so the exclusion is recorded and bounded like every "
                "other one, rather than vanishing into the normalization.")
        if f.score_bound is not None and not f.score_bound[0] <= term <= f.score_bound[1]:
            # The declared bound is not documentation: the margin pruning and truncation
            # bounds recorded in this payload were DERIVED from it, and candidates were
            # discarded on its strength. A factor that exceeds its own bound has already
            # invalidated them, so it fails here rather than publishing them.
            raise ValueError(
                f"scene factor {f.name!r} returned {term!r} for cluster {cluster_id!r}, "
                f"alternative {list(assignment)}, outside its declared score_bound "
                f"{f.score_bound}. Candidates were pruned and alternatives truncated under "
                "that bound, so exceeding it makes the recorded mass bounds false; widen "
                "the declaration or declare None and pay for it in pruning.")
        (prior_terms if f.kind == "scene_prior" else factor_terms)[f.name] = term
        if why:
            reasons[f.name] = list(why)
    return {
        "assignment": [list(pair) for pair in assignment],
        # The linkage this alternative reads the assignment under. Two entries with the same
        # assignment and different links are the two readings of a crossing, and they carry the
        # SAME ``base`` by construction.
        "links": [list(pair) for pair in alt.links],
        "base": base,
        "factor_terms": factor_terms,
        "prior_terms": prior_terms,
        "reasons": reasons,
        # Summed in sorted-name order over both term maps, so a reader recomputing
        # total = fsum([base, *terms-by-sorted-name]) reproduces this float exactly.
        "total": _total(base, factor_terms, prior_terms),
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


def _marginals(refs, entries, rows_of, truncated: bool, unbounded: bool) -> dict:
    """Exact sums over the enumerated alternative set: per arena, P(label) and P(n_modes=k).

    The mode-count marginal is the N-mode ruling's representable uncertainty (H8(e)):
    "three modes, poorly constrained" has to survive as a distribution, not collapse to a
    winner. ``truncated`` travels with the numbers so a reader never mistakes a capped sum
    for a complete one, and ``truncation_unbounded`` travels with it so a reader never mistakes
    an unresolved cluster for a narrowed one — a marginal read out of the payload without its
    cluster is precisely where that distinction would otherwise be lost.
    """
    labels: dict[str, dict[str, float]] = {r: {} for r in refs}
    counts: dict[str, dict[str, float]] = {r: {} for r in refs}
    for e in entries:
        for ref, label in e["assignment"]:
            labels[ref][label] = labels[ref].get(label, 0.0) + e["posterior"]
            k = str(rows_of[(ref, label)].n_modes)
            counts[ref][k] = counts[ref].get(k, 0.0) + e["posterior"]
    return {"label": labels, "n_modes": counts, "truncated": truncated,
            "truncation_unbounded": unbounded}
