"""The scene-factor interface — the declaration, not the physics (SCENE-3 PR 3a).

A scene factor is a term ``ln φ_f(S)`` of the SCENE_DESIGN §2b score. Everything in this
module exists to make the factor ban mechanical rather than a matter of reviewer memory:

* a factor declares its probabilistic ``kind`` (``scene_prior`` or ``structural_likelihood``)
  — there is no undeclared heuristic bonus, because ``validate_factor`` refuses one;
* a factor declares the scene variables it ``consumes``, a ``version``, the tunable constants it
  closes over (``config``) and a bound on its total ln-term (``score_bound``) — and the WHOLE
  declaration enters the revision's ``registry_fingerprint`` as a manifest, so a scored revision
  names the factor set that scored it and cannot be silently re-scored by a retuned one;
* ``score_bound`` is what makes the builder's pruning honest: base-lnZ pruning discards a
  candidate a scoring factor might later have rescued, so it is permitted only within a declared
  bound (see ``isar.scene.build``). An unbounded factor is legal — it just costs the builder its
  pruning, which is the correct price;
* ``couples`` and ``score`` are invoked by the builder with typed objects only —
  ``ComponentObservation`` and the builder's cluster view. Neither carries ``log_evidence``,
  fitted parameters, counts or local posteriors, so a factor *cannot* re-score local data or
  re-apply a prior already priced into ``lnZ`` even by mistake.

The live registry holds the first two physics factors — ``presence`` and ``connectivity``
(SCENE-3 PR 3b). ``RESERVED_FACTOR_NAMES`` reserves the names the remaining ones will take so
two slices cannot pick the same name for different terms.

Stdlib-only, like ``model.py`` — see ``tests/scene/test_import_ban.py``.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from .model import (LINK_TOL_FLOOR, Q_ADJACENCY, Visibility, fingerprint, is_mode, link_tol,
                    q_adjacent, visibility_of)

FACTOR_KINDS = ("scene_prior", "structural_likelihood")

RESERVED_FACTOR_NAMES = ("presence", "connectivity", "zone_centre_approach",
                         "reciprocal_lattice_localization", "scene_prior",
                         "detailed_balance_partner", "topology")


@dataclass(frozen=True, slots=True)
class SceneFactor:
    """One declared term of ``ln W(S)``.

    ``couples(a, b) -> bool`` decides ambiguity-graph edges from two ``ComponentObservation``s
    and must be deterministic and symmetric — the builder calls it on unordered arena pairs
    and a non-deterministic answer would repartition the scene between two identical calls.
    ``score(alternative, cluster_view) -> (ln term, reasons)`` returns the natural-log term
    and the ledger reasons that explain it. ``propose`` is reserved for coherent moves in the
    next task and is ``None`` here.

    ``score_bound`` is ``(lo, hi)`` on the factor's TOTAL ln-term for ANY alternative of ANY
    cluster — ``lo <= score(...) <= hi``, always — or ``None`` for "I will not say". It is a
    promise the builder holds it to: pruning and truncation bounds are derived from it, so a
    factor that declares a bound it can exceed corrupts the stated mass bounds. A neutral factor
    declares ``(0.0, 0.0)``.

    ``config`` is every tunable constant the factor closes over, as sorted ``(name, value)``
    pairs. It exists because a retuned constant is a different factor: without it two revisions
    scored under different tunings would carry the same ``registry_fingerprint`` and look
    replayable against each other.
    """

    name: str
    kind: str
    version: str
    consumes: tuple[str, ...] = ()
    config: tuple[tuple[str, float], ...] = ()
    score_bound: tuple[float, float] | None = None
    couples: Callable | None = None
    score: Callable | None = None
    propose: Callable | None = None


def validate_factor(f: SceneFactor) -> None:
    """Raise ``ValueError`` unless ``f`` is a well-formed declaration."""
    if not isinstance(f, SceneFactor):
        raise ValueError(f"not a SceneFactor: {f!r}")
    if not f.name:
        raise ValueError("scene factor has an empty name")
    if f.kind not in FACTOR_KINDS:
        raise ValueError(
            f"scene factor {f.name!r} declares kind {f.kind!r}; allowed: {FACTOR_KINDS}")
    if not f.version:
        raise ValueError(f"scene factor {f.name!r} declares no version")
    for attr in ("couples", "score", "propose"):
        value = getattr(f, attr)
        if value is not None and not callable(value):
            raise ValueError(f"scene factor {f.name!r} has a non-callable {attr}")
    for pair in f.config:
        if len(pair) != 2 or not isinstance(pair[0], str):
            raise ValueError(f"scene factor {f.name!r} has a malformed config entry {pair!r}: "
                             "expected (name, value) pairs")
    if [k for k, _ in f.config] != sorted(k for k, _ in f.config):
        # Sorted is the canonical form. Unsorted config would fingerprint two identical tunings
        # differently, which is the same silent failure the fingerprint exists to catch.
        raise ValueError(f"scene factor {f.name!r} has unsorted config keys: "
                         f"{[k for k, _ in f.config]}")
    if f.score_bound is not None:
        lo, hi = f.score_bound
        if not (math.isfinite(lo) and math.isfinite(hi)) or lo > hi:
            raise ValueError(f"scene factor {f.name!r} declares score_bound {f.score_bound!r}: "
                             "expected finite (lo, hi) with lo <= hi, or None for unbounded")
    if f.score is None and f.score_bound is not None:
        raise ValueError(f"scene factor {f.name!r} declares a score_bound but has no score")


def factor_manifest(factors) -> list[dict]:
    """The full declaration of every factor in ``factors``, in name order.

    This is what a revision embeds and what ``registry_fingerprint`` digests. Everything that
    changes what the factor set DOES is in here — ``consumes``, ``config`` and ``score_bound``
    included — because a fingerprint over ``(name, kind, version)`` alone would let a factor
    that reads new variables, runs at a new tuning, or promises a new bound re-score a scene
    under an unchanged stamp.

    ``roles`` records which callables are present rather than the callables themselves: a
    function object has no stable identity across processes, but "this factor coupled as well
    as scored" is exactly the structural fact a replay needs to check.
    """
    values = factors.values() if isinstance(factors, dict) else factors
    return sorted(
        ({"name": f.name,
          "kind": f.kind,
          "version": f.version,
          "consumes": list(f.consumes),
          "roles": [r for r in ("couples", "score", "propose") if getattr(f, r) is not None],
          "config": [[k, v] for k, v in f.config],
          "score_bound": None if f.score_bound is None else list(f.score_bound)}
         for f in values),
        key=lambda m: m["name"])


def registry_fingerprint(factors) -> str:
    """Fingerprint of ``factor_manifest(factors)``.

    Accepts the registry mapping or any iterable of factors. Order-free by construction: the
    manifest is sorted by name, so two callers assembling the same factor set in different
    orders stamp the same revision.
    """
    return fingerprint(factor_manifest(factors))


# ----------------------------------------------------------------------------
# The physics constants (nats), owner-tunable
# ----------------------------------------------------------------------------
# Declared v1 structural-likelihood weights: they say how strongly the scene believes the two
# structural claims below, and the SCENE-7 calibration battery will tune them. Every one of them
# is declared in its factor's ``config``, so a retuning moves the registry fingerprint and a
# revision can never be replayed against a differently-tuned one. Honesty about their provisional
# size lives in the ledger: every nonzero contribution is itemized per alternative.
#
# presence — a phonon does not appear at one q and vanish at the next. A neighbouring arena that
# LOOKED and saw nothing where a mode is claimed is evidence against the claim; one that saw
# something there supports it, more weakly (a coincidence of two unrelated features is not rare
# enough to price at the same size as a contradiction).
PRESENCE_MISS_NATS = -1.0
PRESENCE_MATCH_NATS = 0.5
# connectivity — a track that stops mid-cluster where its continuation was searched for and not
# found is a broken track. Priced harder than a presence miss because it is a stronger claim: not
# "something is missing" but "this identified track ends here".
CONNECT_BREAK_NATS = -2.0
# How far the crossing proposer may reach when it asks whether the OTHER pairing is plausible.
# The default chaining tolerance is the resolution width — a track moves less than the resolution
# between neighbouring q or it was never one resolved track. At a branch crossing that bound is
# the wrong physics: the two tracks are exchanging ω order, so the honest question is whether the
# dispersion could carry a track that far over Δq, which is several resolution widths. Refusing to
# propose the swap because it exceeds one resolution width would assign the linkage by ω rank —
# the single failure this factor exists to prevent.
CROSSING_LINK_TOL_FACTOR = 3.0


# ----------------------------------------------------------------------------
# The one cross-arena walk both factors (and the builder's payload annotation) read
# ----------------------------------------------------------------------------
def _by_ref(alt) -> dict:
    out: dict[str, list] = {}
    for c in alt.components:
        out.setdefault(c.scan_ref, []).append(c)
    return {r: sorted(cs, key=lambda c: c.component_id) for r, cs in out.items()}


def _modes(components) -> list:
    """The placeable mode components — a ``None`` ω is a mode with no position to compare."""
    return [c for c in components if is_mode(c) and c.omega is not None]


def neighbour_reports(alt, view) -> tuple[dict, ...]:
    """Every (chosen mode at arena i, adjacent arena j) judgement, in deterministic order.

    One walk, three consumers: ``presence`` prices ``visibility`` (was the mode seen where a
    neighbour looked?), ``connectivity`` prices ``link_visibility`` (does this mode's TRACK
    continue?), and the builder annotates the cluster payload with the same rows so a reader can
    see why an absence was or was not penalized.

    The two visibilities differ in what counts as a match, and only in that: ``visibility`` asks
    whether any chosen mode of ``j`` sits within the linking tolerance, ``link_visibility``
    whether THIS mode is linked to one under the alternative's linkage. Both go through
    :func:`isar.scene.model.visibility_of`, so both inherit the coverage gate: a band the
    neighbour never looked at is censored and is never penalized by either factor.
    """
    chosen = _by_ref(alt)
    reports = []
    for i in view.scan_refs:
        for j in view.scan_refs:
            if i == j:
                continue
            fp_i, fp_j = view.footprints[i], view.footprints[j]
            if not q_adjacent(fp_i.q, fp_j.q):
                continue
            here, there = chosen.get(i, ()), chosen.get(j, ())
            # An elastic or quasielastic component chosen at j hides a band under the elastic
            # line: absence there is not evidence (BLOCKED_UNRESOLVED).
            elastic_j = any(not is_mode(c) for c in there)
            tol = link_tol(fp_i, fp_j)
            partners = _modes(there)
            linked = set(alt.links) | {(b, a) for a, b in alt.links}
            for m in _modes(here):
                w = abs(m.omega)
                matched = any(abs(abs(p.omega) - w) <= tol for p in partners)
                is_linked = any((m.component_id, p.component_id) in linked for p in partners)
                reports.append({
                    "component_id": m.component_id,
                    "scan_ref": i,
                    "neighbour": j,
                    "omega": m.omega,
                    "visibility": str(visibility_of(w, fp_j, matched, elastic_present=elastic_j)),
                    "link_visibility": str(
                        visibility_of(w, fp_j, is_linked, elastic_present=elastic_j)),
                })
    return tuple(reports)


# ----------------------------------------------------------------------------
# presence
# ----------------------------------------------------------------------------
def _presence_score(alt, view):
    """``PRESENCE_MISS_NATS`` per searched-and-missing neighbour, ``PRESENCE_MATCH_NATS`` per hit.

    A function of the cross-arena CONFIGURATION only: it reads no parameter of any single
    observation beyond the component positions the builder already derived, and two alternatives
    that share the same local fits differ here only by which candidates they chose (H11(c),
    H12(a)). Censored and blocked neighbours contribute exactly 0.0 — the coverage gate is where
    "absence is evidence only inside coverage" is actually enforced.
    """
    total, reasons = 0.0, []
    for r in neighbour_reports(alt, view):
        if r["visibility"] == Visibility.SEARCHED_NO_SIGNAL:
            total += PRESENCE_MISS_NATS
            reasons.append(f"miss {r['component_id']} not seen at {r['neighbour']}")
        elif r["visibility"] == Visibility.OBSERVED:
            total += PRESENCE_MATCH_NATS
            reasons.append(f"match {r['component_id']} supported at {r['neighbour']}")
    return total, tuple(reasons)


def _couples(a, b) -> bool:
    """Adjacent arenas whose intrinsic bands overlap — the ambiguity-graph edge both factors use.

    Elastic/quasielastic components sit at ω=0, so two adjacent ``elastic_only`` arenas couple
    through them: a phonon-less arena is part of the scene's structure, not an isolated dead end.
    """
    if not q_adjacent(a.q, b.q) or a.omega is None or b.omega is None:
        return False
    return abs(abs(a.omega) - abs(b.omega)) <= link_tol(a.footprint, b.footprint)


PRESENCE = SceneFactor(
    name="presence", kind="structural_likelihood", version="1",
    consumes=("q", "omega", "footprint"),
    config=(("link_tol_floor", LINK_TOL_FLOOR), ("presence_match_nats", PRESENCE_MATCH_NATS),
            ("presence_miss_nats", PRESENCE_MISS_NATS), ("q_adjacency", Q_ADJACENCY)),
    # Unbounded BY DECLARATION, and the consequence is accepted rather than hidden: the total
    # scales with the number of (mode, neighbour) pairs, so it scales with cluster size, and any
    # static global bound would be a promise this factor can break on a large cluster — which
    # would make the builder's recorded mass bounds false (H12(c)). The price is that margin
    # pruning is disabled while this factor is active; the 4096 cap plus ``truncation_unbounded``
    # honesty covers realistic clusters.
    # ponytail: per-cluster-size bound is the upgrade path — a factor that could declare
    # ``bound(n_arenas, n_components)`` would restore pruning without lying about it.
    score_bound=None,
    couples=_couples, score=_presence_score)


# ----------------------------------------------------------------------------
# connectivity
# ----------------------------------------------------------------------------
def _connectivity_score(alt, view):
    """``CONNECT_BREAK_NATS`` per broken track. Penalties only — no smoothness reward.

    A break is a track whose next arena LOOKED where the track would have continued and found
    nothing to link to. Two break-free linkages therefore score EQUAL: at a symmetric crossing
    the swap and the straight-through come out 50/50, which is the honest answer. Rewarding the
    smoother linkage would be exactly the "lowest is acoustic" rule wearing a factor's clothes,
    and the intensity anomalies that really do discriminate an eigenvector exchange are constraint
    transport's business, not this factor's.
    """
    total, reasons = 0.0, []
    for r in neighbour_reports(alt, view):
        if r["link_visibility"] == Visibility.SEARCHED_NO_SIGNAL:
            total += CONNECT_BREAK_NATS
            reasons.append(f"break {r['component_id']} has no continuation at {r['neighbour']}")
    return total, tuple(reasons)


def _junction_pairings(alt, view):
    """``[(current_links, alternative_links), ...]`` for every ambiguous 2×2 junction.

    The crossing ambiguity in its pure form: two modes at each of two adjacent arenas, and two
    ways to connect them — rank-preserving and rank-crossing. Both are proposed whenever both are
    reachable within ``CROSSING_LINK_TOL_FACTOR × link_tol``; the alternative that is not already
    in the linkage becomes a proposal.

    ponytail: exactly 2×2 junctions only. Three modes crossing at one q is a real but rarer
    configuration; the upgrade path is the full permutation set, bounded by the same cap.
    """
    chosen = _by_ref(alt)
    current = set(alt.links)
    out = []
    for a in range(len(view.scan_refs)):
        for b in range(a + 1, len(view.scan_refs)):
            i, j = view.scan_refs[a], view.scan_refs[b]
            fp_i, fp_j = view.footprints[i], view.footprints[j]
            if not q_adjacent(fp_i.q, fp_j.q):
                continue
            here = sorted(_modes(chosen.get(i, ())), key=lambda c: (abs(c.omega), c.component_id))
            there = sorted(_modes(chosen.get(j, ())), key=lambda c: (abs(c.omega), c.component_id))
            if len(here) != 2 or len(there) != 2:
                continue
            tol = CROSSING_LINK_TOL_FACTOR * link_tol(fp_i, fp_j)
            straight = frozenset({(here[0].component_id, there[0].component_id),
                                  (here[1].component_id, there[1].component_id)})
            crossed = frozenset({(here[0].component_id, there[1].component_id),
                                 (here[1].component_id, there[0].component_id)})
            reachable = [p for p, spans in ((straight, ((0, 0), (1, 1))),
                                            (crossed, ((0, 1), (1, 0))))
                         if all(abs(abs(here[u].omega) - abs(there[v].omega)) <= tol
                                for u, v in spans)]
            if len(reachable) < 2:
                continue
            ids = {c.component_id for c in here} | {c.component_id for c in there}
            mine = frozenset(l for l in current if l[0] in ids and l[1] in ids)
            for pairing in reachable:
                if pairing != mine:
                    out.append((mine, pairing))
    return out


def _propose_crossings(alt, view):
    """Alternative LINK SETS for ``alt``, given its default linkage. Deterministic, deduplicated.

    Every combination of junction swaps is offered; the builder caps how many it keeps and records
    the truncation. The candidate assignment is untouched — a linkage variant shares its base
    Σ lnZ exactly, so only factor terms can separate the two readings of a crossing.
    """
    junctions = _junction_pairings(alt, view)
    variants, links = set(), set(alt.links)
    for n in range(1, len(junctions) + 1):
        for combo in _combinations(junctions, n):
            swapped = set(links)
            for mine, other in combo:
                swapped -= mine
                swapped |= other
            variants.add(tuple(sorted(swapped)))
    variants.discard(tuple(sorted(links)))
    return tuple(sorted(variants))


def _combinations(items, n):
    """``itertools.combinations`` without the import — the module is deliberately import-thin."""
    if n == 0:
        yield ()
        return
    for i, item in enumerate(items):
        for rest in _combinations(items[i + 1:], n - 1):
            yield (item, *rest)


CONNECTIVITY = SceneFactor(
    name="connectivity", kind="structural_likelihood", version="1",
    consumes=("q", "omega", "footprint", "links"),
    config=(("connect_break_nats", CONNECT_BREAK_NATS),
            ("crossing_link_tol_factor", CROSSING_LINK_TOL_FACTOR),
            ("link_tol_floor", LINK_TOL_FLOOR), ("q_adjacency", Q_ADJACENCY)),
    # Unbounded for the same reason as ``presence``: breaks are counted per (mode, neighbour) pair.
    # ponytail: per-cluster-size bound is the upgrade path.
    score_bound=None,
    couples=_couples, score=_connectivity_score, propose=_propose_crossings)


# The live registry: the builder falls back to it when a caller passes no explicit factor set.
FACTORS: dict[str, SceneFactor] = {f.name: f for f in (PRESENCE, CONNECTIVITY)}
