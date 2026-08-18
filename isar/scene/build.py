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
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from isar.schema.statistics import CONSTRAINT_ORIGINS
from isar.schema.steering import CANONICAL_MASTER, EVIDENCE_SINKS

from .factors import (FACTORS, SceneFactor, factor_manifest, neighbour_reports,
                      registry_fingerprint, validate_factor)
from .model import (NON_MODE_ID_TAGS, SCENE_SCHEMA, CandidateRow, ComponentObservation,
                    ConditionSet, Footprint, PathTopology, SampleKnowledge, SceneObservation,
                    Visibility, canonical_bytes, digest_bytes, fingerprint, footprint_of, is_mode,
                    link_reach, path_adjacent, path_topology)
from .model import partition_id as model_partition_id
from .model import partition_key as model_partition_key

# SCENE-4 PR 4.0 (#21): the lane split over isar.schema.statistics.CONSTRAINT_ORIGINS. A
# candidate's provenance is DECLARED (prespecified relative to this scan, exactly-once by
# construction) or it is DATA-DERIVED transported conditioning (accumulated from other scans'
# counts, the exactly-once risk H204 named) — every member of the schema's enum is one or the
# other, and the assertion below makes a THIRD, unaccounted-for member a loud import-time failure
# rather than a silent fall into whichever lane happened to have an ``in`` check written first.
SCORING_CONSTRAINT_ORIGINS = frozenset(
    {"none", "user_declared", "prespecified_scientific", "catalogue_variant"})
TRANSPORTED_CONSTRAINT_ORIGINS = frozenset(
    {"prior_predictive", "data_dependent_rescue", "post_selection", "mixed"})
# PR #30 review response (disposition 16): an unconditional ``raise`` rather than ``assert`` — an
# ``assert`` disappears under ``python -O``, which would silently let a third, unaccounted-for
# CONSTRAINT_ORIGINS member fall through to whichever lane's ``in`` check happened to run first,
# exactly the silent-fallthrough failure this check exists to prevent.
if SCORING_CONSTRAINT_ORIGINS | TRANSPORTED_CONSTRAINT_ORIGINS != CONSTRAINT_ORIGINS:
    raise ValueError(
        "isar.schema.statistics.CONSTRAINT_ORIGINS has a member neither scoring lane accounts for — "
        "the scene intake lane split (#21) must be updated before this schema change is safe")

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
# (``linkage_truncated``, ``linkage_dropped_prior_mass``) and, because a dropped reading can carry
# factor terms the kept ones do not and nothing bounds them, it puts the cluster in the same
# UNRESOLVED lane as ``truncation_unbounded``.
MAX_LINKAGE_VARIANTS = 8

# The builder's own declared prior, and the failure it prevents. Linkage variants REFINE a
# candidate assignment; they must never add prior mass to it. Without this term an assignment that
# happens to admit two linkage readings carries twice the unnormalized weight of a rival assignment
# with one — ln 2 nats nothing paid for, and worse, an amount set by an enumeration knob (the
# proposer's reachability tolerance) rather than by the data. It is the owner's N-mode precedent
# applied to connectivity: enumerating a hypothesis is no probabilistic boost, a uniform prior over
# its refinements dilutes and never boosts.
#
# The denominator is the number of linkage readings ENUMERATED for that assignment, before the cap,
# not the number kept (review finding 10). Normalizing by the kept count would silently redistribute
# a dropped reading's prior mass over its survivors, so a truncated assignment would carry exactly
# the weight of an untruncated one and the truncation would be invisible in the arithmetic. Dividing
# by the total instead leaves the kept variants summing to n_kept/n_total < 1, and the missing share
# is recorded per cluster as ``linkage_dropped_prior_mass``.
#
# Declared like any other term (it enters the manifest and the registry fingerprint) but applied by
# the builder rather than by a factor: only the builder knows how many linkages an assignment
# ended up with, and a factor able to see that count could score on the enumeration itself.
#
# ``score_bound=None`` follows from that denominator: n_total is however many readings the
# proposers offered, which no constant bounds, so −ln(n_total) has no floor to declare. The price
# is stated where it is paid — ``_score_span`` returns ``None`` (no base-lnZ pruning, no stated
# truncation bound) whenever a proposing factor is active.
LINKAGE_PRIOR = SceneFactor(
    name="linkage_prior", kind="scene_prior", version="2", consumes=("links",),
    config=(("max_linkage_variants", float(MAX_LINKAGE_VARIANTS)),),
    score_bound=None)


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

    ``sample`` is the DECLARED sample knowledge (never derived), threaded through the view rather
    than closed over by the factors: the factor singletons stay frozen and their registry
    fingerprints stay declaration-only, so two revisions of the same registry under different
    declarations are told apart by the revision's ``sample_knowledge`` block, not by a factor set
    that quietly differs.

    ``refiners`` are the registry's ``refine_visibility`` hooks as ``(factor name, hook)``
    pairs — the name travels with the callable so a hook that breaks the refinement rule can be
    named in the error rather than left for a reader to identify by elimination; ``reports_cache`` memoizes
    :func:`isar.scene.factors.neighbour_reports` for the life of this view (one cluster of one
    build) so its three consumers walk the arenas once between them. It is scoped to the view on
    purpose — a module-level cache would outlive the build and hand one revision's reports to the
    next.

    ponytail: the memo retains one report tuple per ENUMERATED alternative for the whole cluster
    (up to ``SCENE_ALT_CAP`` × ``MAX_LINKAGE_VARIANTS`` entries), because the leading-alternative
    annotation runs after every alternative has scored. Bounded and deliberate; scoping the cache
    to the scoring pass and re-deriving the leader's reports afterwards is the upgrade path if a
    large cluster's peak memory ever matters.
    """

    cluster_id: str
    scan_refs: tuple[str, ...]
    components: MappingProxyType   # scan_ref -> tuple[ComponentObservation, ...]
    footprints: MappingProxyType   # scan_ref -> Footprint
    sample: SampleKnowledge = SampleKnowledge()
    refiners: tuple = ()
    reports_cache: dict = field(default_factory=dict, compare=False, repr=False)


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
                sample: SampleKnowledge | None = None,
                margin_nats: float | None = None,
                alt_cap: int | None = None,
                condition_t_round: float | None = 0.1) -> SceneRevision:
    """Score ``observations`` into a ``SceneRevision``. Pure, deterministic, replayable.

    ``factors`` defaults to the live ``FACTORS`` registry; ``catalogue_version`` is a
    caller-supplied stamp, because the scene never imports the catalogue. ``sample`` is the
    scientist's DECLARED sample knowledge (:func:`isar.workspace_api.scene.sample_knowledge`
    lifts it off the card); ``None`` means nothing was declared, which is a legal scene in which
    the knowledge-consuming factors are inert and say so in ``sample_knowledge.degradations``.

    ``condition_t_round`` (SCENE-4 PR 4.0, #28) is the temperature-binning width the PARTITION key
    compares by — the same policy field ``isar.reduce.collate`` used to pool this workspace's raw
    scans (``card.policy.collation["t_round"]``), so two arenas coupled at the scene layer are
    exactly the ones Stage II would have treated as the same condition. ``0.1`` reproduces
    collate.py's own default for a caller that declares nothing.

    Payload alignment contract: a cluster's ``score_ablation[name]`` is a POSITIONAL list,
    index-aligned with that cluster's ``alternatives`` — ``score_ablation[name][i]`` is the
    posterior of ``alternatives[i]`` with factor ``name``'s score terms removed. The payload
    states this as ``score_ablation_alignment`` so an external consumer cannot silently read it
    as a mapping. ``transformation_ablation[name]`` (SCENE-4 PR 4.0, #22) is the same shape for a
    REFINER named ``name``: the posterior of ``alternatives[i]`` with that refiner's visibility
    rewrite removed and every scoring factor rerun against the unrewritten visibility — a
    distinct ledger from ``score_ablation`` because a refiner has no score term to zero out; see
    ``_transformation_ablation``'s docstring. ``transformation_ablation_alignment`` states the
    same positional contract.
    """
    # Constants resolve AT CALL TIME, never as default-argument captures: a default evaluated
    # at import binds the module constant's value forever, so monkeypatching or an
    # owner-retuned constant would silently not apply to callers that omit the argument.
    margin = SCENE_PRUNE_MARGIN if margin_nats is None else float(margin_nats)
    cap = SCENE_ALT_CAP if alt_cap is None else int(alt_cap)
    if cap < 1:
        raise ValueError(f"alt_cap must be >= 1, got {cap}")
    factor_list = _factor_list(FACTORS if factors is None else factors)
    declared = SampleKnowledge() if sample is None else sample

    obs = _validated(observations)
    # PR 4.0 (#26): a count-untrusted arena (``count_trust`` off "trusted" — a gate refusal
    # recorded as ATTEMPTED, never a measurement) was never actually MEASURED, so it takes no
    # part in the path, the footprints, or any cluster below — unlike an arena whose counts are
    # trusted but whose local comparison could not score (still measured, still footprinted, see
    # the note below). It is folded into ``non_scoring`` directly, with no footprint entry.
    trusted_obs = tuple(o for o in obs if o.count_trust == "trusted")
    untrusted_reasons = [{"scan_ref": o.scan_ref, "reason": "count_untrusted"}
                         for o in obs if o.count_trust != "trusted"]

    # PR 4.0 (#25/#28): PARTITION first, before any path/topology/cluster arithmetic — a
    # cross-partition edge is not a rare mistake to prune later, it is not a fact the geometry
    # below is even allowed to compute. Two arenas of different sample identity, different binned
    # condition, or different lattice epoch share nothing downstream: not a path, not a footprint
    # adjacency, not a cluster, not a normalization.
    partitions: dict[tuple, list[SceneObservation]] = {}
    for o in trusted_obs:
        partitions.setdefault(_partition_key(o, condition_t_round), []).append(o)

    # The ordered path FIRST within each partition: every adjacency, every declared-geometry
    # coordinate and every edge length downstream is read off it, and it is a property of the
    # arena SET (review finding 3) — now the arena set of ONE partition, never the whole scene.
    #
    # Over every TRUSTED, TOPOLOGY-ELIGIBLE arena of the partition, not only the scoring ones: the
    # path is the experiment map, and an arena whose evidence may not be summed was still MEASURED
    # at its q. A ``"context_probe"``-tagged arena (#25) is withheld from the topology input
    # entirely — it never perturbs the path's collinearity check — but still receives a footprint
    # below, with no path ordinal (``path_index=None``), so it can neither collapse the path nor
    # accidentally chain to another off-path arena through it.
    footprints: dict[str, Footprint] = {}
    topologies: list[PathTopology] = []
    for key, members in partitions.items():
        pid = _partition_id(key)
        eligible = {o.scan_ref: o.q for o in members if o.topology_role != "context_probe"}
        topology = path_topology(eligible)
        topologies.append(topology)
        for o in members:
            footprints[o.scan_ref] = footprint_of(o, topology, partition_id=pid)

    scoring, non_scoring = _split_by_evidence(trusted_obs)
    non_scoring = sorted(non_scoring + untrusted_reasons, key=lambda d: d["scan_ref"])
    # Order matters and is the contract: evidence split → margin pruning → components of the
    # SURVIVORS → coupling edges → clusters → enumeration. A candidate that will not be
    # enumerated must not shape the partition either: an excluded candidate's component could
    # otherwise couple two arenas and merge two clusters, and since truncation and normalization
    # are joint over a cluster, a candidate nobody scores would still move everyone's posterior.
    span = _score_span(factor_list, scoring, footprints)
    pruning = {o.scan_ref: _prune(o, margin, span) for o in scoring}
    components = {o.scan_ref: _components_of(o, footprints[o.scan_ref],
                                             _survivor_labels(pruning[o.scan_ref]))
                  for o in scoring}

    # #28: clustering runs PER PARTITION, never across one — the ambiguity graph and the
    # normalization it feeds must not see two arenas of different sample/condition/lattice-epoch
    # as candidates for the same edge. ``_clusters`` already builds its ``itertools.combinations``
    # only over the ``refs`` it is handed, so scoping the call scopes the coupling search.
    scoring_by_partition: dict[tuple, list[SceneObservation]] = {}
    for o in scoring:
        scoring_by_partition.setdefault(_partition_key(o, condition_t_round), []).append(o)
    clusters = sorted(
        (cl for members in scoring_by_partition.values()
         for cl in _clusters(members, components, footprints, factor_list)),
        key=lambda refs: refs[0])

    by_ref = {o.scan_ref: o for o in scoring}
    # PR #30 review response (disposition 12): every arena of ONE cluster shares one partition
    # (``_partition_key`` includes ``lattice_epoch``), so any single member's epoch is the
    # cluster's. A cluster whose epoch does not match the CURRENTLY declared
    # ``SampleKnowledge.lattice_epoch`` is scored against a blank sample instead — see
    # ``_cluster_sample``'s docstring for why this, rather than filtering the factor list, is what
    # keeps ``zone_centre_approach``/``reciprocal_lattice_localization`` inert for it.
    cluster_payloads = [
        _score_cluster(refs, by_ref, pruning, components, footprints, factor_list, cap, span,
                       _cluster_sample(by_ref[refs[0]], declared))
        for refs in clusters]
    epoch_mismatch = any(
        by_ref[refs[0]].lattice_epoch is not None and declared.lattice_epoch is not None
        and by_ref[refs[0]].lattice_epoch != declared.lattice_epoch
        for refs in clusters)

    # #28: a temperature present with no declared binning policy leaves that arena's condition key
    # unresolved (``ConditionSet.temperature_K_binned=None``) — legal, and recorded once here
    # rather than per-partition, since it is a fact about the DECLARATION, not about any one
    # partition's geometry.
    # PR #30 review response (disposition 15): the SAME threshold ``ConditionSet.of`` fails closed
    # at (``>= 1e-6``, not merely ``> 0.0``) — a ``condition_t_round`` between 0 and 1e-6 already
    # makes every temperature-bearing arena's key unresolved there, so this ledger check must agree
    # or it would silently omit the reason exactly in that band.
    condition_binning_unavailable = (
        not (condition_t_round is not None and float(condition_t_round) >= 1e-6)
        and any(dict(o.conditions).get("temperature_K") is not None for o in trusted_obs))

    payload = {
        "schema": SCENE_SCHEMA,
        "catalogue_version": str(catalogue_version),
        "registry_fingerprint": registry_fingerprint(_declared(factor_list)),
        # The declarations that fingerprint stands for, in full, so a reader can see WHAT scored
        # the scene rather than only that something did.
        "factor_manifest": factor_manifest(_declared(factor_list)),
        # The values ACTUALLY used, not the module constants: a revision built with a tuned
        # margin must say so, or a replay would silently use a different model.
        "margin_nats": margin,
        "alt_cap": cap,
        # The values ACTUALLY used for the #28 partition's condition binning — see
        # ``condition_t_round``'s docstring above.
        "condition_t_round": condition_t_round,
        # Σ_f (hi_f − lo_f) over the active scoring factors — the nats of rescue the pruner had
        # to allow for. ``None`` = at least one factor declared no bound, so no base-lnZ pruning
        # or truncation bound was permitted at all.
        "score_span_nats": span,
        # Declares how to read every cluster's score_ablation lists: positionally, against that
        # cluster's ``alternatives``.
        "score_ablation_alignment": "by_alternative_index",
        # Same alignment contract for #22's transformation_ablation lists — declared separately
        # so a consumer that reads one payload key never has to assume the other's convention.
        "transformation_ablation_alignment": "by_alternative_index",
        # Constraint-transport provenance: declared and dormant (the transport channel lives
        # in local evidence, per the PR-a design), so the field exists before it is filled.
        "transport": None,
        # What the scientist DECLARED about the sample, and which declared-input capabilities
        # were consequently inert. It is part of the digested payload, so declaring knowledge
        # moves the revision digest even where it changes no number — a scene scored against a
        # zone period is not the same scene as one scored blind to it.
        "sample_knowledge": {**declared.to_dict(),
                             "degradations": _degradations(factor_list, declared, topologies,
                                                          condition_binning_unavailable,
                                                          epoch_mismatch)},
        # PR #30 review response (disposition 11, W5): the FULL observation, not only
        # ``{scan_ref, fingerprint, candidates}`` — sample, conditions, count_trust,
        # lattice_epoch and topology_role were already hashed INTO the fingerprint but were not
        # reconstructable from the stored payload, so an auditor reading a revision back could not
        # recover what partitioned it without rejoining against the workspace that built it (which
        # may since have moved on). Digest move: every existing revision's digest changes.
        "observations": [o.to_dict() for o in obs],
        "non_scoring": non_scoring,
        "footprints": [footprints[o.scan_ref].to_dict() for o in trusted_obs],
        "clusters": cluster_payloads,
    }
    return SceneRevision.of(payload)


def _partition_key(o: SceneObservation, t_round: float | None) -> tuple:
    """The #25/#28 partition identity — ONE implementation, in the model layer.

    Moved to :func:`isar.scene.model.partition_key` (PR #31 finding K) so a reader attributing
    an arena to its exact partition — ``completeness_relative`` scoping non-scoring arenas —
    computes the same identity the builder stamps, byte for byte. Two arenas whose keys differ
    are, by construction, never adjacent, never clustered, never normalized together.
    """
    return model_partition_key(o.sample, o.conditions, o.lattice_epoch, o.scan_ref, t_round,
                               o.topology_role, o.path_fingerprint)


def _partition_id(key: tuple) -> str:
    """See :func:`isar.scene.model.partition_id` — the one digest of a partition key."""
    return model_partition_id(key)


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
    if LINKAGE_PRIOR.name in names:
        # The builder applies that term itself; a second one under the same name would be summed
        # twice into every total and would be indistinguishable in the ledger.
        raise ValueError(f"scene factor name {LINKAGE_PRIOR.name!r} is reserved for the builder's "
                         "per-assignment linkage prior")
    return tuple(sorted(values, key=lambda f: f.name))


def _declared(factor_list) -> tuple:
    """What SCORED the scene: the caller's factors plus the builder's own declared prior."""
    return tuple(sorted([*factor_list, LINKAGE_PRIOR], key=lambda f: f.name))


def _cluster_sample(leader: SceneObservation, declared: SampleKnowledge) -> SampleKnowledge:
    """PR #30 review response (disposition 12): declared sample knowledge, scoped to ONE cluster.

    ``leader`` is any one member of the cluster (every arena of a cluster shares one partition,
    and ``_partition_key`` includes ``lattice_epoch``, so its epoch is the cluster's own). A
    cluster measured under a lattice epoch that is not the one CURRENTLY declared must not receive
    the CURRENT frame's zone/reflection semantics — a stale-epoch arena's elastic feature is not
    necessarily near this epoch's zone centre or reflection, and scoring it as though it were would
    silently misattribute a re-orientation.

    Handing the mismatched cluster a BLANK ``SampleKnowledge()`` — rather than filtering
    ``zone_centre_approach``/``reciprocal_lattice_localization`` out of that cluster's
    ``factor_list`` — is what keeps every cluster scored by the same declared factor SET: the
    payload's ``registry_fingerprint``/``factor_manifest`` stay global and honest (a revision
    with a mismatched cluster still says which factors COULD have scored, same as one without),
    and the factors' own ``_zone_centres_declared``/``sample.reflections`` checks then see
    nothing declared and go inert on THIS cluster exactly the way they already do for a workspace
    that declared nothing at all — no change to ``factors.py`` needed.

    A match requires BOTH sides to actually declare an epoch and disagree — ``leader.lattice_epoch
    is None`` (no production writer stamps it yet: PR 4.1-4.3 wire the campaign-provenance
    producer, per ``SceneObservation.topology_role``'s docstring) is "not yet known", not "known
    to differ", so it is treated as compatible with whatever epoch IS declared rather than as a
    mismatch — the ``H13`` null-coupling rule is a statement about which arenas may share a
    PARTITION, and does not extend to demanding a stamp that does not exist yet before a
    workspace may use declared knowledge at all. Only an arena EXPLICITLY stamped a DIFFERENT
    epoch than the one currently declared is withheld.
    """
    if leader.lattice_epoch is None or declared.lattice_epoch is None:
        return declared
    return declared if leader.lattice_epoch == declared.lattice_epoch else SampleKnowledge()


def _degradations(factor_list, declared: SampleKnowledge, topologies: list[PathTopology],
                  condition_binning_unavailable: bool = False,
                  epoch_mismatch: bool = False) -> list[str]:
    """Every reason a declared capability was inert on this scene, sorted and deduplicated.

    The honesty this buys: a capability with nothing declared to work on must neither raise nor
    quietly change what the scene believes, so the only trace it can leave is a ledger entry.
    Without one, a revision built against a card that declares no zone period is
    indistinguishable from one where the zone factor ran and found no endpoints.

    ``topologies`` is one entry per #28 PARTITION (:func:`build_scene`); a reason is recorded once
    for the whole scene the moment ANY partition triggers it — this is a per-scene ledger, not a
    per-partition one, so a reader learns THAT some part of the scene degraded and reads the
    per-arena ``footprints``/``partition_id`` to find which.

    Three of the reasons are the builder's rather than a factor's, because they are facts about an
    arena SET that no ``degradations(sample)`` hook can see (review finding 3):

    * ``path_topology_unavailable`` — some partition's q set is not a path, so there is no
      ordering and no adjacency THERE. The scene still classifies and localizes per arena; it
      prices no cross-arena absence at all within that partition, which is the fail-closed arm
      rather than a guessed ordering.
    * ``zone_geometry_inapplicable_on_path`` / ``lattice_geometry_inapplicable_on_path`` — some
      partition's path is collinear but does not run along a reciprocal axis, so the card's
      SCALAR period and reflection positions (declared in rlu along an axis) have no coordinate
      there. Recorded only when the corresponding declaration exists, since an undeclared one
      already names itself.
    * ``condition_binning_unavailable`` (#28) — an arena declares a raw temperature but the caller
      declared no (or a non-positive) ``condition_t_round``, so that arena's PARTITION key could
      not be resolved and it fails closed to no-coupling (:class:`isar.scene.model.ConditionSet`).
    * ``knowledge_epoch_mismatch`` (PR #30 review response, disposition 12) — at least one cluster
      was EXPLICITLY measured under a ``lattice_epoch`` other than the one CURRENTLY declared
      (both sides stamped, and different — an unstamped, ``None`` epoch is "not yet known" and is
      never this), so :func:`_cluster_sample` handed it a blank ``SampleKnowledge`` and its
      ``zone_centre_approach``/``reciprocal_lattice_localization`` semantics were withheld for
      that cluster specifically — a per-scene ledger entry, like the other two builder reasons,
      because it is a fact about a CLUSTER that no ``degradations(sample)`` hook can see.
    """
    reasons = {reason for f in factor_list if f.degradations is not None
               for reason in f.degradations(declared)}
    for topology in topologies:
        if not topology.collinear:
            reasons.add("path_topology_unavailable")
        elif topology.index and topology.axis is None:
            # ``topology.index`` non-empty: with no placeable arena at all there is no path to be
            # off the axis of, and the per-component ``unplaceable_q`` state already names that.
            if declared.zone_period is not None:
                reasons.add("zone_geometry_inapplicable_on_path")
            if declared.reflections:
                reasons.add("lattice_geometry_inapplicable_on_path")
    if condition_binning_unavailable:
        reasons.add("condition_binning_unavailable")
    if epoch_mismatch:
        reasons.add("knowledge_epoch_mismatch")
    return sorted(reasons)


def _score_span(factor_list, scoring_obs, footprints) -> float | None:
    """``Σ_f (hi_f − lo_f)`` over the ACTIVE SCORING factors, or ``None`` if any is unbounded.

    This is the total nats by which factor terms can reorder two alternatives that the base
    Σ lnZ has already separated. Everything the builder discards without enumerating it — a
    margin-pruned candidate, a capped alternative — is discarded on base alone, so this span is
    exactly the slack those decisions must carry, and its absence is exactly the reason to stop
    discarding. A coupling-only factor is irrelevant here: it changes the partition, not the
    score, and cannot rescue anything.

    SCENE-4 PR 4.0 (#22): a REFINER (``refine_visibility``) is not a coupling-only factor even
    when it declares no ``score`` — it rewrites what a scoring factor reads, so a rescued cluster
    of reports can move the total by up to ``rescue_bound_per_report × N`` nats, where ``N`` is a
    CONSERVATIVE upper bound on how many reports any alternative could have had refined, counted
    over every USABLE candidate of the whole build BEFORE any pruning (:func:`_max_refinable_reports`)
    — a global, pre-partition count is a superset of any one cluster's true count, so it can only
    WIDEN the pruning margin, never narrow it (H12(c): bounded, never optimistic). A refiner that
    will not declare ``rescue_bound_per_report`` is treated exactly like an unbounded scoring
    factor: the whole span collapses to ``None`` and margin pruning is disabled outright,
    joining the ``truncation_unbounded`` lane semantics — an unresolved cluster, not a narrowed
    one, is the only honest posture when a refiner's worst case cannot be bounded.
    """
    scorers = [f for f in factor_list if f.score is not None]
    if any(f.score_bound is None for f in scorers):
        return None
    refiners = [f for f in factor_list if f.refine_visibility is not None]
    if any(f.rescue_bound_per_report is None for f in refiners):
        return None
    refiner_span = (math.fsum(f.rescue_bound_per_report
                              for f in refiners) * _max_refinable_reports(scoring_obs, footprints)
                    if refiners else 0.0)
    # LINKAGE_PRIOR has no ``score`` callable (the builder applies it) but it is a term of the
    # total all the same, so its declared span belongs in the slack the pruner must allow for —
    # and only then. With no proposing factor every assignment has exactly one linkage, the term
    # is identically 0.0 across the cluster, and widening the margin for it would discard nothing
    # differently while changing every recorded bound.
    if not any(f.propose is not None for f in factor_list):
        linkage = 0.0
    elif LINKAGE_PRIOR.score_bound is None:
        # −ln(n_total) over however many readings the proposers offered: no constant bounds it, so
        # there is no honest slack to hand the pruner. Same posture as an unbounded scoring factor.
        return None
    else:
        lo, hi = LINKAGE_PRIOR.score_bound
        linkage = hi - lo
    return math.fsum([linkage, refiner_span, *(h - l for l, h in (f.score_bound for f in scorers))])


def _max_refinable_reports(scoring_obs, footprints) -> int:
    """Conservative upper bound on refinable ``(mode, adjacent neighbour)`` reports, GLOBAL to the
    whole build and computed BEFORE any pruning (#22) — "every usable candidate" the plan text
    means literally, not one cluster's share of it.

    One report per mode component of the arena's most-mode-heavy USABLE candidate (only one
    candidate is ever CHOSEN per arena by any one alternative, so the max over its candidates
    already bounds what one alternative could contribute) times the number of OTHER scoring
    arenas it is path-adjacent to. Both factors overcount relative to any single alternative —
    an alternative chooses one candidate, not the richest one, and links at most one mode per
    neighbour, not every mode to every neighbour — and the sum runs over the whole build rather
    than one cluster. All three are overcounts in the SAFE direction: a larger ``N`` only widens
    the margin the pruner must allow, never narrows it.

    Structurally always obtainable here (component descriptors and footprint adjacency are both
    persisted, pre-pruning facts) — see ``_score_span``'s docstring for the case this codebase
    actually models as "unobtainable": a refiner that declines to bound even one report.
    """
    total = 0
    for o in scoring_obs:
        max_modes = max((sum(1 for d in c.components if d.kind == "mode")
                         for c in o.candidates if _usable_lnz(c) is not None), default=0)
        if max_modes == 0:
            continue
        fp_o = footprints[o.scan_ref]
        # ``path_adjacent`` alone compares raw ``path_index`` ordinals, which are assigned
        # PER PARTITION (#28) — two different partitions can each start numbering at 0, so an
        # unguarded adjacency check could call arenas of unrelated partitions "neighbours" purely
        # by ordinal coincidence. The partition_id equality is the same fail-closed boundary the
        # rest of the builder holds arenas to (H13): nothing crosses it, including this count.
        neighbours = sum(1 for other in scoring_obs
                         if other.scan_ref != o.scan_ref
                         and footprints[other.scan_ref].partition_id == fp_o.partition_id
                         and path_adjacent(fp_o, footprints[other.scan_ref]))
        total += max_modes * neighbours
    return total


# ----------------------------------------------------------------------------
# 2. Evidence-bearing split
# ----------------------------------------------------------------------------
def _usable_lnz(c: CandidateRow) -> float | None:
    """The candidate's lnZ if it may be summed, else ``None``. ±inf is not a number to sum."""
    if not c.usable or c.log_evidence is None or not math.isfinite(c.log_evidence):
        return None
    return float(c.log_evidence)


def _arena_constraint_origin(o: SceneObservation) -> str:
    """The #21 arena-level ``CONSTRAINT_ORIGINS`` verdict, over every USABLE candidate.

    "Usable" (:func:`_usable_lnz` is not ``None``) rather than every candidate in the row: an
    excluded/no-evidence candidate can never be summed into any alternative's ``ln W(S)``
    (:func:`_prune`), so its provenance carries no exactly-once risk for this arena — only a
    candidate that COULD be chosen (this arena enumerates every surviving candidate as an
    alternative, not only ``best_label``) can taint the comparison.

    NOT a reduction across candidates the way ``isar.analyze.loop.aggregate_constraint_origin``
    reduces across ONE fit's own constraint list: there, several constraint terms jointly shape a
    SINGLE lnZ, so one data-derived term among them taints that one number and the fit-level
    aggregate correctly folds to ``"mixed"``. Here, two DIFFERENT candidates are two SEPARATE,
    independently-computed numbers — a ``"none"``-origin candidate sitting beside a
    ``"catalogue_variant"``-origin candidate in the same arena is not a mixed anything, it is two
    cleanly-classified alternatives, and collapsing that heterogeneity to ``"mixed"`` would
    non-score an arena no candidate of which is actually tainted. The correct arena rule is ANY:
    one candidate carrying transported (or already fit-level-``"mixed"``) provenance taints the
    whole arena, because THAT candidate's lnZ could be the one an alternative sums — so a single
    unsafe usable candidate decides the lane, never a set-reduction over however many candidates
    happen to differ.

    Raises when a usable candidate's ``constraint_origin`` is the tri-state ``None`` (no semantics
    record — a delivery from before this contract existed) or a string outside
    ``isar.schema.statistics.CONSTRAINT_ORIGINS`` (schema drift) — REFUSE, per the #21 ruling,
    never a silent lane guess.
    """
    origins = [c.constraint_origin for c in o.candidates if _usable_lnz(c) is not None]
    if any(g is None for g in origins):
        raise ValueError(
            f"scene: {o.scan_ref!r} carries a usable candidate with no constraint_origin "
            "semantics record — this workspace predates persisted fit semantics; re-deliver or "
            "rebuild it before SCENE analysis")
    unknown = sorted({g for g in origins if g not in CONSTRAINT_ORIGINS})
    if unknown:
        raise ValueError(
            f"scene: {o.scan_ref!r} carries a candidate with constraint_origin {unknown!r}, not "
            "a member of isar.schema.statistics.CONSTRAINT_ORIGINS — schema drift: re-deliver or "
            "rebuild this workspace before SCENE analysis")
    unsafe = sorted(g for g in origins if g in TRANSPORTED_CONSTRAINT_ORIGINS)
    return unsafe[0] if unsafe else "none"


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
        # A TRUSTED measurement (PR 4.0 #26) with no usable interpretation yet — not a broken
        # arena, and not the same claim as "excluded"/"held"/"undersampled": the counts are good,
        # nothing has been fitted to them (or was fitted and produced no candidate at all).
        return "no_interpretation"
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
    # SCENE-4 PR 4.0 (#21, Variant A): the WINNING comparison's constraint provenance decides
    # scoring eligibility, evaluated only now that the arena would otherwise score. See
    # ``_arena_constraint_origin`` for why this is an ARENA verdict over every candidate that
    # could actually be summed, not the single chosen candidate's origin alone.
    origin = _arena_constraint_origin(o)
    if origin in TRANSPORTED_CONSTRAINT_ORIGINS:
        return "transported_conditioning"
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
            kind=kind,
            q=o.q,
            omega=omega,
            footprint=fp,
            visibility=Visibility.OBSERVED)
        for c in o.candidates if c.label in labels
        for tag, kind, omega in _component_slots(c))


def _component_slots(c: CandidateRow):
    """``(id tag, kind, ω)`` for one candidate, from its PERSISTED component descriptors.

    The descriptors are the single authority on what a candidate is made of — they were derived
    from the enumerated structure the candidate was fitted as, at the writer. Nothing here reads
    the label: a label is an identifier, and recovering kinds from its text would make a display
    string load-bearing for physics and would silently reclassify a structure the day the grammar
    grows a suffix.

    A mode whose slot has no ω (an unplaceable position, or a candidate with no fit result at all)
    still emits its component with ``omega=None``: the mode exists, it is merely unplaceable.
    """
    for d in c.components:
        if d.kind == "mode":
            w = c.omegas[d.slot] if 0 <= d.slot < len(c.omegas) else None
            yield str(d.slot), d.kind, (None if w is None else float(w))
        else:
            yield NON_MODE_ID_TAGS[d.kind], d.kind, 0.0


# ----------------------------------------------------------------------------
# 4. Ambiguity graph → clusters
# ----------------------------------------------------------------------------
def _clusters(scoring, components, footprints, factor_list) -> list[tuple[str, ...]]:
    """Connected components of the ambiguity graph, recomputed from scratch every call.

    Two kinds of edge, and the second exists because the first alone got the physics backwards.
    ``couples`` needs a component on BOTH sides, so an arena whose surviving candidates are all
    background-only could never join a cluster — and a track disappearing into a searched,
    covered, entirely empty neighbour went unpriced, while the same track disappearing into a
    neighbour that happened to hold something else was priced. ``couples_arena`` closes that:
    a component may couple to a whole neighbouring ARENA through its footprint. Only SCORING
    arenas are considered on either side, so an H7-incomplete or evidence-less arena stays
    experiment-map-only — coverage never resurrects one.

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
    arena_couplers = [f.couples_arena for f in factor_list if f.couples_arena is not None]
    if couplers or arena_couplers:
        # ponytail: O(arenas² × components²) all-pairs probe. Fine at campaign scale (tens of
        # arenas); a q-bucketed index is the upgrade path if a scene ever holds thousands.
        for a, b in itertools.combinations(refs, 2):
            if find(a) == find(b):
                continue
            if any(couples(ca, cb)
                   for couples in couplers
                   for ca in components[a] for cb in components[b]):
                parent[find(a)] = find(b)
            elif any(couples(c, footprints[other])
                     for couples in arena_couplers
                     for this, other in ((a, b), (b, a))
                     for c in components[this]):
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
    """``(assignments, n_possible, truncated, log_discarded_mass_bound, unbounded)`` for a cluster.

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
        # Nothing was dropped, so there is no bound to state — in log space the honest value is
        # −inf, which strict JSON cannot carry. ``truncated=False`` is what says so instead, and
        # it is the flag a reader checks first (see the cluster payload).
        return sorted(assignments), n_possible, False, None, False

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
    return (sorted(tuple(sorted(a)) for a in kept), n_possible, True,
            _log_discarded_bound(n_possible - len(kept), lowest_base, leader_base, span), False)


def _log_discarded_bound(n_dropped: int, lowest_base: float, leader_base: float,
                         span: float = 0.0) -> float:
    """LOG of an upper bound on the mass a cap dropped: ``ln n_dropped + lowest − leader + span``.

    Conservative and stated. Every dropped alternative D has ``base(D) ≤ lowest`` (the kept set
    is the top of the base ordering) and, for the best kept alternative L with ``base(L) =
    leader``,

        ln W(D) − ln W(L) = base(D) − base(L) + Σ_f (φ_f(D) − φ_f(L))
                          ≤ (lowest − leader) + Σ_f (hi_f − lo_f) = lowest − leader + span,

    so each dropped alternative's unnormalized weight relative to the leader is at most
    ``exp(lowest − leader + span)``, and there are ``n_dropped`` of them. ``span = 0`` is the
    no-scoring-factor case and reproduces the bare ``n_dropped × exp(lowest − leader)``.

    STATED IN LOG SPACE, and that is the recorded quantity (review finding E). The linear bound
    overflowed to a stated ``inf`` — a vacuous number that a reader glancing at
    ``discarded_mass_bound`` could take for a bounded approximation, when it says exactly nothing.
    The log-space value is finite for every finite input, so the bound never has to be
    downgraded to "absent" by arithmetic, and the only unbounded lane left is the real one
    (``truncation_unbounded``: no factor declared a bound, or a linkage truncation dropped
    readings nothing can bound). ``n_dropped`` is an exact Python int over the full product and
    can exceed the float range; ``math.log`` takes the big int exactly, where multiplying would
    raise ``OverflowError`` on the conversion.

    A reader that wants the mass takes ``exp`` of it and accepts the underflow to 0.0 that the
    linear form would have had anyway; a bound ≥ 0 in log space (≥ 1 in mass) is a true bound
    that constrains nothing, which is the point at which the operator raises ``alt_cap``.
    """
    return math.log(n_dropped) + (lowest_base - leader_base) + span


# ----------------------------------------------------------------------------
# 6b. Linkage variants (the connectivity variable)
# ----------------------------------------------------------------------------
def _default_links(alt: SceneAlternative, view: ClusterView) -> tuple[tuple[str, str], ...]:
    """Nearest-ω chaining between CONSECUTIVE arenas: the linkage assumed unless a factor proposes.

    Deterministic and greedy: for each consecutive arena pair on the derived path (review finding
    3) in scan_ref order, each mode of the lower arena takes the closest unclaimed mode of the
    higher one within ``link_reach`` (ties by component id). A mode with no partner in reach stays
    UNLINKED rather than being forced onto a distant one — a forced link would invent a track the
    data does not support, and an unlinked mode is exactly what presence prices as an unsupported
    claim.

    Reach is the resolution floor PLUS the declared dispersion slope over the edge (review finding
    5). Chaining on the resolution width alone said a branch may not move further between two q
    than two peaks must be apart to be told apart — an energy-resolution number standing in for a
    dispersion bound, which is not a bound on it at all.
    """
    chosen: dict[str, list] = {}
    for c in alt.components:
        chosen.setdefault(c.scan_ref, []).append(c)
    links = []
    for a in range(len(view.scan_refs)):
        for b in range(a + 1, len(view.scan_refs)):
            i, j = view.scan_refs[a], view.scan_refs[b]
            fp_i, fp_j = view.footprints[i], view.footprints[j]
            if not path_adjacent(fp_i, fp_j):
                continue
            tol = link_reach(fp_i, fp_j, view.sample.dispersion_slope_max)
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
    """``(kept variants, n_enumerated)`` — the linkage readings of ONE candidate assignment.

    The default chaining always comes first, so a truncation can never drop it. Proposals come
    from the factors' ``propose`` hooks (the crossing proposer), are deduplicated, and are ordered
    deterministically before the cap applies.

    ``n_enumerated`` is the count BEFORE the cap and it is what ``LINKAGE_PRIOR`` divides by, so a
    dropped reading's prior mass stays dropped instead of being redistributed over the survivors
    (review finding 10). ``n_enumerated > len(kept)`` is the truncation.
    """
    default = _validated_links(_default_links(alt, view), alt)
    proposed = set()
    for f in factor_list:
        if f.propose is None:
            continue
        for links in f.propose(replace(alt, links=default), view):
            variant = _validated_links(tuple(sorted(tuple(pair) for pair in links)), alt)
            if variant != default:
                proposed.add(variant)
    ordered = [default, *sorted(proposed)]
    return tuple(ordered[:MAX_LINKAGE_VARIANTS]), len(ordered)


def _validated_links(links, alt: SceneAlternative):
    """Return ``links``, or raise if a component continues as more than one track in a direction.

    A track is one continuation per neighbouring arena: a component linked to two components of
    the SAME arena is not a reading of the data, it is a linkage that cannot exist. Such a set
    would score like any other and publish real posterior mass, so it is refused at the one place
    every emitted linkage passes through — the default chaining and every proposal alike — rather
    than trusted to whichever proposer produced it.
    """
    seen = set()
    for pair in links:
        for tail, head in (pair, pair[::-1]):
            key = (tail, head.split("#", 1)[0])
            if key in seen:
                raise ValueError(
                    f"scene linkage for cluster {alt.cluster_id!r}, alternative "
                    f"{list(alt.assignment)}: component {tail!r} is linked to more than one "
                    f"component of arena {key[1]!r}. A component continues as at most one track "
                    "per adjacent arena; a proposer that emits otherwise is broken.")
            seen.add(key)
    return links


# ----------------------------------------------------------------------------
# 7-9. Scoring, ledger, marginals
# ----------------------------------------------------------------------------
def _score_cluster(refs, by_ref, pruning, components, footprints, factor_list, cap,
                   span, declared: SampleKnowledge) -> dict:
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
                       footprints=MappingProxyType({r: footprints[r] for r in refs}),
                       sample=declared,
                       refiners=tuple((f.name, f.refine_visibility) for f in factor_list
                                      if f.refine_visibility is not None))
    lnz_of = {(r, c.label): lnz for r in refs for c, lnz in pruning[r]["survivors"]}
    rows_of = {(r, c.label): c for r in refs for c in by_ref[r].candidates}

    entries = []
    linkage_dropped = 0.0
    for assignment in assignments:
        chosen = {ref: label for ref, label in assignment}
        # ONE lnZ per arena. A chosen candidate with n_modes=2 contributes its evidence once;
        # its modes are components below, and components carry no evidence at all.
        base = math.fsum(lnz_of[(ref, label)] for ref, label in assignment)
        assigned = SceneAlternative(
            cluster_id=cluster_id, assignment=assignment,
            components=tuple(c for ref in refs for c in components[ref]
                             if c.candidate_label == chosen[ref]))
        variants, n_linkages = _linkage_variants(assigned, view, factor_list)
        # The share of THIS assignment's linkage prior the cap dropped. Exact, not a bound: the
        # prior is uniform over the n enumerated readings by construction, so k kept readings
        # retain exactly k/n of it. Reported per cluster as the worst assignment's share, which is
        # the honest scalar — the assignments' own weights are only known after normalization, and
        # weighting a dropped-prior statement by a posterior computed without that prior would be
        # circular.
        linkage_dropped = max(linkage_dropped, (n_linkages - len(variants)) / n_linkages)
        for links in variants:
            entries.append(_score_alternative(replace(assigned, links=links), view, base,
                                              factor_list, cluster_id, n_linkages))

    # A dropped linkage reading can carry factor terms none of the kept ones carry (the crossing
    # proposer's pairings score differently whenever they link different modes), and no ordering
    # bounds those terms — linkage variants share a base Σ lnZ exactly, so there is no base
    # ranking to bound them by. A linkage-truncated cluster is therefore UNRESOLVED in the same
    # sense ``truncation_unbounded`` already names, and it joins that lane rather than getting a
    # quieter flag of its own: the marginals a reader lifts out must not look narrowed.
    linkage_truncated = linkage_dropped > 0.0
    if linkage_truncated:
        truncated, unbounded, discarded = True, True, None

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

    # SCENE-4 PR 4.0 (#22): distinct from score_ablation above — see
    # ``_transformation_ablation``'s docstring for why a refiner needs a rerun, not a term zeroed.
    transformation_ablation = _transformation_ablation(entries, view, refs, components,
                                                        factor_list, cluster_id)

    return {
        "cluster_id": cluster_id,
        "scan_refs": list(refs),
        "truncated": truncated,
        "n_enumerated": len(entries),
        "n_assignments": len(assignments),
        "n_possible": n_possible,
        # ln of the upper bound on the posterior mass the enumeration cap dropped, relative to
        # the leader. LOG space is the recorded form (review finding E): the linear bound
        # overflowed to a vacuous ``inf`` that a reader could take for a bounded approximation,
        # while this one is finite for every finite input.
        #
        # ``None`` means no number, and ``truncated`` says which kind of no-number it is: FALSE
        # means nothing was dropped (the bound would be −inf, which strict JSON cannot carry);
        # TRUE means nothing bounds what was dropped — no factor declared a bound, or a linkage
        # truncation dropped readings that no base ordering ranks — and ``truncation_unbounded``
        # below is then True as well. There is no third cause: the arithmetic cannot overflow.
        "log_discarded_mass_bound": discarded,
        # True when the enumerated set is not the complete one AND nothing bounds what was
        # dropped — an unbounded scoring factor over the alternative cap, or a linkage truncation.
        # The cluster is UNRESOLVED, not narrowed, and no reader may treat these posteriors as a
        # bounded approximation of the full ones.
        "truncation_unbounded": unbounded,
        # True when some assignment had more linkage readings than MAX_LINKAGE_VARIANTS.
        "linkage_truncated": linkage_truncated,
        # The largest share of any single assignment's linkage prior that the cap dropped, exactly
        # (n_enumerated − n_kept)/n_enumerated. 0.0 when nothing was dropped. It is a statement
        # about PRIOR mass, not posterior mass: the dropped readings' factor terms are unknown, so
        # this bounds nothing about the posterior and ``truncation_unbounded`` above is what says
        # so.
        "linkage_dropped_prior_mass": linkage_dropped,
        "pruning": {r: pruning[r]["excluded"] for r in refs},
        "log_norm": log_norm,
        "alternatives": entries,
        # Why each chosen mode's absence at a neighbour was, or was not, priced — for the leading
        # alternative only, which is what a reader is looking at. Additive: a consumer that does
        # not know the key is unaffected.
        "visibility": (list(neighbour_reports(_leader(entries, refs, components, cluster_id), view))
                       if entries else []),
        # Belief-neutral per-component labels from the ``classify`` hooks, by factor name. A
        # classification says what a component IS relative to declared knowledge; it enters no
        # total, so it can never be mistaken for a term that moved a posterior.
        "classification": {f.name: f.classify(view)
                           for f in factor_list if f.classify is not None},
        "score_ablation": score_ablation,
        # #22: per-refiner posterior with that refiner's visibility rewrite removed, index-aligned
        # with ``alternatives`` like ``score_ablation`` (payload
        # ``transformation_ablation_alignment``). Empty when no factor in the registry refines.
        "transformation_ablation": transformation_ablation,
        "marginals": _marginals(refs, entries, rows_of, truncated, unbounded),
    }


def _alt_of(entry: dict, cluster_id: str, refs, components) -> SceneAlternative:
    """Rebuild the ``SceneAlternative`` a ledger ``entry`` was scored from.

    Shared by ``_leader`` and the #22 transformation ablation, both of which need to re-derive a
    typed alternative from an already-scored ledger row rather than a second enumeration.
    """
    assignment = tuple(tuple(pair) for pair in entry["assignment"])
    chosen = {ref: label for ref, label in assignment}
    return SceneAlternative(
        cluster_id=cluster_id, assignment=assignment,
        components=tuple(c for ref in refs for c in components[ref]
                         if c.candidate_label == chosen[ref]),
        links=tuple(tuple(pair) for pair in entry["links"]))


def _leader(entries, refs, components, cluster_id) -> SceneAlternative:
    """The highest-posterior alternative, rebuilt as a ``SceneAlternative``. Ties by position."""
    best = max(entries, key=lambda e: e["posterior"])
    return _alt_of(best, cluster_id, refs, components)


def _score_terms(alt: SceneAlternative, view: ClusterView, factor_list, cluster_id: str,
                 assignment) -> tuple[dict, dict, dict]:
    """``(factor_terms, prior_terms, reasons)`` from every SCORING factor's ``score(alt, view)``.

    Split out of ``_score_alternative`` (SCENE-4 PR 4.0, #22) because the transformation ablation
    reruns exactly this loop under a view with one refiner's ``refine_visibility`` removed —
    ``presence`` reads the view's REFINED visibility, so its term cannot be zeroed out after the
    fact the way ``score_ablation`` zeroes a factor's own term; it has to be recomputed against a
    scene that never made the rewrite.
    """
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
    return factor_terms, prior_terms, reasons


def _score_alternative(alt: SceneAlternative, view: ClusterView, base: float, factor_list,
                       cluster_id: str, n_linkages: int) -> dict:
    """One ledger entry: the terms of ``alt`` and the total they add up to.

    ``n_linkages`` is how many linkage readings this ALTERNATIVE'S ASSIGNMENT was enumerated
    with, BEFORE any cap; ``LINKAGE_PRIOR`` spreads one assignment's prior mass uniformly over
    them (see its declaration) so that refining an assignment never boosts it, and so that a
    capped assignment keeps only the share of its prior it still carries readings for.
    """
    assignment = alt.assignment
    factor_terms, prior_terms, reasons = _score_terms(alt, view, factor_list, cluster_id,
                                                       assignment)
    prior_terms[LINKAGE_PRIOR.name] = -math.log(n_linkages)
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


def _transformation_ablation(entries, view: ClusterView, refs, components, factor_list,
                             cluster_id: str) -> dict:
    """SCENE-4 PR 4.0 (#22): per REFINER, the posterior over the FIXED alternative set with that
    refiner's ``refine_visibility`` hook removed.

    A distinct ledger from ``score_ablation`` (H12(f) untouched in meaning): score_ablation asks
    what a factor's SCORE TERM did, holding the alternative set and every OTHER term fixed
    including the refined visibility. A refiner declares no score term to remove — it rewrites
    what ``presence`` reads — so the only honest question is what the scene would have said had
    the rewrite never happened, which needs the visibility RECOMPUTED, not a term zeroed out.
    Everything else stays fixed exactly like ``score_ablation`` fixes it: the enumerated
    alternatives (no re-enumeration), each alternative's ``base`` Σ lnZ, and its linkage-prior
    share (the linkage variable does not depend on refined visibility).

    Index-aligned with ``entries``, like ``score_ablation`` — the payload's
    ``transformation_ablation_alignment`` says so for a reader who has not seen this docstring.
    Empty when the registry carries no refiner, so a scene scored under ``presence`` alone (no
    zone/lattice factors) publishes no misleading entries.
    """
    refiners = [f for f in factor_list if f.refine_visibility is not None]
    out: dict[str, list[float]] = {}
    for rf in refiners:
        ablated = replace(view,
                          refiners=tuple((n, r) for n, r in view.refiners if n != rf.name),
                          reports_cache={})
        totals = []
        for e in entries:
            alt = _alt_of(e, cluster_id, refs, components)
            factor_terms, prior_terms, _ = _score_terms(alt, ablated, factor_list, cluster_id,
                                                         alt.assignment)
            # The linkage share is a fact about the ASSIGNMENT's enumerated readings, never about
            # refined visibility — held at its original value rather than recomputed.
            prior_terms[LINKAGE_PRIOR.name] = e["prior_terms"][LINKAGE_PRIOR.name]
            totals.append(_total(e["base"], factor_terms, prior_terms))
        norm = _logsumexp(totals)
        out[rf.name] = [math.exp(t - norm) for t in totals]
    return out


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
