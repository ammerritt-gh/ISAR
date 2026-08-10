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

The live registry holds the four physics factors of SCENE-3 PR 3b. Only ``presence`` scores;
``connectivity`` supplies the ambiguity edges and the crossing proposals, and
``zone_centre_approach`` / ``reciprocal_lattice_localization`` read DECLARED sample knowledge and
only reinterpret a visibility state or classify a component. None of the three moves belief, so
none of them costs the builder its pruning. ``RESERVED_FACTOR_NAMES`` reserves the names the
remaining ones will take so two slices cannot pick the same name for different terms.

The factorization, and why no null is charged twice (review findings 7 and 8)
----------------------------------------------------------------------------
Every ln-term of a scene alternative is charged against one ``(mode m at arena i, adjacent arena
j)`` report — the rows :func:`neighbour_reports` emits, one per ordered adjacent pair per chosen
mode. There is exactly ONE such row per (mode, neighbour), it carries exactly ONE visibility
state, and exactly ONE factor reads it. That is the whole argument, and it is checkable three
ways:

1. **One judgement per row.** A row's ``visibility`` is a single
   :class:`~isar.scene.model.Visibility`, so its five states partition the possibilities: an
   ``observed`` row is a match, a ``searched_no_signal`` row is a miss, and the other three are
   free. No arithmetic anywhere adds two states of one row.
2. **One reader.** ``presence`` is the only factor in the registry with a ``score``. There is no
   second scoring factor that could look at the same row, so a null cannot be summed twice even
   in principle. This is what the review's finding 8 bought: ``connectivity`` used to charge
   ``CONNECT_BREAK_NATS`` on the *absence of a neighbouring component*, which is the same null
   ``presence`` was already charging as a miss. That term is gone rather than re-homed — a
   neighbour that looked and found nothing is a detection fact, and detection is presence's.
   ``connectivity`` was left with no term that discriminates two readings of the SAME observed
   components (the crossing variants link every mode on both sides, so they differ nowhere a
   topology-only term could see), so it honestly declares ``score=None`` rather than inventing
   one to stay busy.
3. **Support follows the linkage, so one peak supports one mode.** ``has_match`` for the row is
   "m is LINKED to a component at j under THIS alternative", never "some component at j sits
   within tolerance of m" (finding 7). The builder's structural degree guard makes the linkage
   one-to-one per adjacent arena pair, so a single observed peak at j can be the support of at
   most one mode at i; the second mode claiming it is unlinked and is charged (or censored, or
   blocked) on its own coverage facts. The proximity match that allowed two claims on one peak
   is deleted, not kept alongside — a second assignment channel could disagree with the linkage
   the alternative asserts.

The one consequence worth stating plainly: a miss that is linkage-INDEPENDENT (no reachable
partner at j at all, so the mode is unlinked under every reading) contributes identically to
every linkage variant of its assignment, because those variants share their components and
differ only in links the unreachable mode cannot carry. It is charged once per (mode, neighbour)
in each variant and therefore cancels out of the within-assignment comparison entirely.

Stdlib-only, like ``model.py`` — see ``tests/scene/test_import_ban.py``.
"""
from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from dataclasses import dataclass

from .model import (LINK_TOL_FLOOR, Q_ADJACENCY, Visibility, blocks_elastic_line, covers,
                    fingerprint, is_mode, link_tol, path_coordinate, q_adjacent, visibility_of)

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

    ``classify``, ``refine_visibility``, ``couples_arena`` and ``degradations`` are the
    non-scoring hooks (SCENE-3 PR 3b, task 2). They exist because not every structural claim is
    a ln-term: some of them name what a component IS, or what an absence MEANS, or say that a
    declared capability had nothing to work with. Each is recorded in ``factor_manifest``'s
    ``roles``, so a factor that grows one is a different declaration and fingerprints as one.

    * ``classify(view) -> dict[component_id, state]`` — a per-component label the builder folds
      into the cluster payload. It moves no belief; a factor that wants to move belief declares
      a ``score``.
    * ``refine_visibility(report, view) -> str | None`` — given one neighbour report, a
      replacement visibility state or ``None``. The builder enforces that it may only replace
      the two absence states: an ``observed`` mode stays observed and a coverage gap stays a
      no-claim, whatever a factor believes.
    * ``couples_arena(component, footprint) -> bool`` — an ambiguity-graph edge from a component
      to a whole neighbouring ARENA rather than to one of its components. It is what lets a
      searched-but-empty arena join the cluster whose track disappears into it.
    * ``degradations(sample) -> tuple[str, ...]`` — why this factor is inert under the declared
      sample knowledge. Absent declarations must never raise and never silently change scoring,
      so the ledger is where they show up.
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
    classify: Callable | None = None
    refine_visibility: Callable | None = None
    couples_arena: Callable | None = None
    degradations: Callable | None = None


# Every optional callable of ``SceneFactor``, in the order ``factor_manifest`` reports them.
HOOKS = ("couples", "score", "propose", "classify", "refine_visibility", "couples_arena",
         "degradations")


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
    for attr in HOOKS:
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
          "roles": [r for r in HOOKS if getattr(f, r) is not None],
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
# LOOKED and found nothing to link this mode to is evidence against the claim; one that supplied
# the mode's linked continuation supports it, more weakly (a coincidence of two unrelated features
# is not rare enough to price at the same size as a contradiction).
PRESENCE_MISS_NATS = -1.0
PRESENCE_MATCH_NATS = 0.5
# connectivity declares no weight of its own. It had one — CONNECT_BREAK_NATS, charged when a
# mode's track had no continuation at a searched neighbour — and that is the SAME null presence
# charges as a miss (review finding 8). Deleted rather than retuned: see the factorization block
# in the module docstring.
#
# How far the crossing proposer may reach when it asks whether the OTHER pairing is plausible.
# The default chaining tolerance is the resolution width — a track moves less than the resolution
# between neighbouring q or it was never one resolved track. At a branch crossing that bound is
# the wrong physics: the two tracks are exchanging ω order, so the honest question is whether the
# dispersion could carry a track that far over Δq, which is several resolution widths. Refusing to
# propose the swap because it exceeds one resolution width would assign the linkage by ω rank —
# the single failure this factor exists to prevent.
CROSSING_LINK_TOL_FACTOR = 3.0

# zone_centre_approach — how close to a declared zone centre an arena must SIT before the
# gapless-branch termination argument applies to it. This is a statement about q positioning,
# not about dispersion: an arena is *at* a zone centre or it is not. The default is well under
# the 0.05 rlu scan-grid step ISAR rounds HKL to (``CardPolicy.hkl_round_step``), so "near the
# centre" can never spread to the neighbouring grid node — the failure that would turn a
# zone-local exemption into a blanket amnesty for every absence in the neighbourhood. It is
# necessarily far tighter than Q_ADJACENCY (0.15), which is the NEIGHBOUR scale.
ZONE_CENTRE_Q_TOL = 0.02
# ... and how much energy a gapless branch may plausibly have shed between the arena where the
# mode was seen and the zone centre where it terminates. Not the resolution: a branch descending
# to ω→0 over Δq moves by however much the dispersion carries it, which is several resolution
# widths — the same physics as CROSSING_LINK_TOL_FACTOR. Deliberately tighter than that 3.0,
# because this factor grants an absence AMNESTY rather than proposing a hypothesis: where the
# crossing proposer errs toward offering both readings, this one errs toward still penalizing.
ENDPOINT_OMEGA_TOL_FACTOR = 2.0

# reciprocal_lattice_localization — how far from a declared reflection an elastic feature may sit
# and still BE that reflection. One scan-grid step (``CardPolicy.hkl_round_step``, 0.05 rlu):
# the arena either sits on the reflection's grid node or it does not, and the width of a real
# Bragg peak in q (0.08 rlu in the world suite) comfortably contains one step. Two steps away is
# a different position on the path, not a mis-centred reflection.
# ponytail: one tolerance for every reflection. A real peak's q width is instrument- and
# sample-dependent, so an arena two grid steps out on the tail of a broad reflection classifies
# ``off_lattice`` here; a declared per-reflection width is the upgrade path, and it is a
# declaration to add to the card rather than a number for this module to infer.
BRAGG_Q_TOL = 0.05


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

    ONE row per (chosen mode, adjacent arena), carrying ONE ``visibility`` — which is what makes
    the exactly-once claim of the module docstring checkable rather than asserted. Two consumers:
    ``presence`` prices the state, and the builder annotates the cluster payload with the same
    rows so a reader can see why an absence was or was not penalized.

    A mode is supported at ``j`` when it is LINKED to a component there under this alternative's
    linkage, and by nothing else (review finding 7). The independent proximity match this walk
    used to compute alongside — "some chosen mode of ``j`` sits within ``link_tol``" — is gone: it
    was a second assignment channel that could disagree with the linkage the alternative asserts,
    and because it was many-to-one it let two modes at ``i`` both claim one observed peak at ``j``
    as support for their existence.

    The state goes through :func:`isar.scene.model.visibility_of`, so it inherits the coverage
    gate: a band the neighbour never looked at is censored and is never penalized. It is then
    offered to the view's ``refine_visibility`` hooks, which is where declared sample knowledge
    can turn an unexplained absence into a ``structural_endpoint``. Computed ONCE per
    (alternative, view) and memoized on the view: the consumers used to run this walk three times
    over identical inputs.
    """
    cached = view.reports_cache.get(alt)
    if cached is not None:
        return cached
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
            # TWO facts, because they answer two different questions and conflating them made a
            # quasielastic feature argue Bragg physics. ``elastic_present`` is the strict one: an
            # elastic component is chosen at j, which is what the zone-centre endpoint argument
            # needs (a branch descending to ω→0 is swallowed by the ELASTIC response).
            # ``blocking_present`` is the detectability one: elastic or quasielastic, either of
            # which hides a low-energy band and makes its absence uninformative.
            elastic_j = any(c.kind == "elastic" for c in there)
            blocking_j = any(blocks_elastic_line(c) for c in there)
            partners = _modes(there)
            linked = set(alt.links) | {(b, a) for a, b in alt.links}
            for m in _modes(here):
                w = abs(m.omega)
                is_linked = any((m.component_id, p.component_id) in linked for p in partners)
                report = {
                    "component_id": m.component_id,
                    "scan_ref": i,
                    "neighbour": j,
                    "omega": m.omega,
                    # Carried as FACTS of the report rather than recomputed by every refiner: a
                    # second derivation of either would be a second physics. A refiner that needs
                    # the Bragg-adjacent claim reads ``elastic_present``; the visibility below
                    # keys on ``blocking_present``, which is detectability alone.
                    "elastic_present": elastic_j,
                    "blocking_present": blocking_j,
                    "visibility": visibility_of(w, fp_j, is_linked, blocking_present=blocking_j),
                }
                reports.append({**report,
                                "visibility": str(_refined(report["visibility"], report, view))})
    out = tuple(reports)
    view.reports_cache[alt] = out
    return out


# The only states a ``refine_visibility`` hook may replace. An OBSERVED mode was SEEN and no
# declared structure may argue it away; a CENSORED band was never looked at, and a factor
# turning a coverage gap into any other state would manufacture a measurement. Enforced here,
# once, rather than trusted to each refiner.
REFINABLE = (Visibility.SEARCHED_NO_SIGNAL, Visibility.BLOCKED_UNRESOLVED)


def _refined(state, report, view):
    """``state`` after the view's refiners, first non-``None`` answer wins."""
    if state not in REFINABLE:
        return state
    for name, refine in view.refiners:
        out = refine(report, view)
        if out is None:
            continue
        # Checked as a VALUE before it becomes a Visibility: ``Visibility("nonsense")`` raises the
        # enum's own message, which names neither the factor nor the report, so a broken refiner
        # in a growing registry would have to be found by elimination.
        if out not in REFINABLE and out != Visibility.STRUCTURAL_ENDPOINT:
            raise ValueError(
                f"scene factor {name!r} returned {out!r} for {report['component_id']!r} at "
                f"{report['neighbour']!r}: a refiner may only reinterpret an absence "
                f"({[str(s) for s in REFINABLE]}) or terminate it as "
                f"{Visibility.STRUCTURAL_ENDPOINT!s}. Turning an absence into an observation, or "
                "a coverage gap into anything, would manufacture a measurement.")
        return Visibility(out)
    return state


# ----------------------------------------------------------------------------
# presence
# ----------------------------------------------------------------------------
def _presence_score(alt, view):
    """``PRESENCE_MISS_NATS`` per searched-and-missing neighbour, ``PRESENCE_MATCH_NATS`` per hit.

    The ONE scoring term of the live registry, and the sole reader of a neighbour report — which
    is what makes "no null is charged twice" structural here rather than a review promise (see the
    module docstring). Exactly one term per row, and at most one row per (chosen mode, adjacent
    arena).

    A function of the cross-arena CONFIGURATION only: it reads no parameter of any single
    observation beyond the component positions and the linkage the builder already derived, and
    two alternatives that share the same local fits differ here only by which candidates they
    chose and how those components are linked (H11(c), H12(a)). Censored and blocked neighbours
    contribute exactly 0.0 — the coverage gate is where "absence is evidence only inside coverage"
    is actually enforced.
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


def _couples_arena(c, fp) -> bool:
    """Does a mode at one arena have a stake in what a neighbouring ARENA saw? (Carried item 15.)

    ``couples`` needs a component on both sides, so an arena whose surviving candidates are all
    background-only emitted nothing and could never join a cluster — and the scene was therefore
    SILENT exactly where absence evidence is strongest: a track disappearing into a neighbour
    that looked hard and found nothing at all went unpriced, while a track disappearing into a
    neighbour that found *something else* was priced. Inverted physics.

    The edge this adds is deliberately narrow: the neighbour must be q-adjacent AND its footprint
    must COVER the mode's band. No coverage is no claim, so it grows no cluster — the same gate
    ``visibility_of`` applies, reused rather than restated. Deterministic on footprints and
    discrete component positions only.
    """
    return (is_mode(c) and c.omega is not None
            and q_adjacent(c.q, fp.q) and covers(fp, abs(c.omega)))


PRESENCE = SceneFactor(
    # v3: support for a claimed mode now derives from the alternative's LINKAGE rather than from an
    # independent any-partner-within-tolerance proximity match (review finding 7), so a v2 revision
    # scored one observed peak as support for every nearby mode and is not reproducible here. (v2
    # was coverage coupling via ``couples_arena``, which changed which arenas share a cluster.)
    name="presence", kind="structural_likelihood", version="3",
    consumes=("q", "omega", "footprint", "links"),
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
    couples=_couples, couples_arena=_couples_arena, score=_presence_score)


# ----------------------------------------------------------------------------
# connectivity
# ----------------------------------------------------------------------------
def _junction_pairings(alt, view):
    """``[(current_links, (alternative_pairing, ...)), ...]`` — ONE entry per ambiguous junction.

    The crossing ambiguity in its pure form: two modes at each of two adjacent arenas, and two
    ways to connect them — rank-preserving and rank-crossing. Both are considered whenever both
    are reachable within ``CROSSING_LINK_TOL_FACTOR × link_tol``; whichever is not already in the
    linkage becomes an alternative for that junction.

    A junction's alternatives are MUTUALLY EXCLUSIVE, which is why one junction is one entry.
    Emitting them as two independent entries let a caller apply both to the same junction: the
    second application would remove the first's links again and leave a linkage in which one mode
    continues as two tracks — an impossible reading, published with real posterior mass.

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
            alternatives = tuple(p for p in reachable if p != mine)
            if alternatives:
                out.append((mine, alternatives))
    return out


def _propose_crossings(alt, view):
    """Alternative LINK SETS for ``alt``, given its default linkage. Deterministic, deduplicated.

    Every combination of junctions is offered, with exactly ONE alternative pairing chosen per
    junction in the combination — a junction's pairings are mutually exclusive, so applying two of
    them to the same junction would produce a mode continuing as two tracks. The builder caps how
    many variants it keeps, records the truncation, and validates every emitted link set. The
    candidate assignment is untouched: a linkage variant shares its base Σ lnZ exactly, so only
    factor terms can separate the two readings of a crossing.
    """
    junctions = _junction_pairings(alt, view)
    variants, links = set(), set(alt.links)
    for n in range(1, len(junctions) + 1):
        for combo in itertools.combinations(junctions, n):
            for choice in itertools.product(*(alternatives for _, alternatives in combo)):
                swapped = set(links)
                for (mine, _), pairing in zip(combo, choice):
                    swapped -= mine
                    swapped |= pairing
                variants.add(tuple(sorted(swapped)))
    variants.discard(tuple(sorted(links)))
    return tuple(sorted(variants))


# v2: the factor no longer SCORES. Its ``CONNECT_BREAK_NATS`` term charged the absence of a
# neighbouring component, which presence already charges as a miss — the same null in two factors
# (review finding 8). Presence keeps it, because a neighbour that looked and found nothing is a
# detection fact. What connectivity was left to own is topology among components that EXIST on
# both sides, and on the readings this registry enumerates there is nothing there to price: a
# crossing's two pairings link every mode at both arenas, so they are indistinguishable to any
# term that may not look at an absence. So it declares no score rather than a manufactured one,
# and a v1 revision's connectivity terms are not reproducible under it.
#
# It remains a factor, and a load-bearing one: ``couples`` supplies ambiguity-graph edges and
# ``propose`` supplies the crossing variants that make the linkage a scene variable at all. A
# factor that shapes the hypothesis space without pricing it is exactly what the ``roles``
# manifest exists to make visible.
CONNECTIVITY = SceneFactor(
    name="connectivity", kind="structural_likelihood", version="2",
    consumes=("q", "omega", "footprint", "links"),
    config=(("crossing_link_tol_factor", CROSSING_LINK_TOL_FACTOR),
            ("link_tol_floor", LINK_TOL_FLOOR), ("q_adjacency", Q_ADJACENCY)),
    # No score, therefore no bound to declare, and ``_score_span`` ignores it entirely.
    score_bound=None,
    couples=_couples, propose=_propose_crossings)


# ----------------------------------------------------------------------------
# zone_centre_approach
# ----------------------------------------------------------------------------
def _zone_centres_declared(sample) -> bool:
    return sample.zone_period is not None


def _at_zone_centre(h: float, period: float) -> bool:
    """Is the path coordinate ``h`` a multiple of ``period``, to within the declared tolerance?

    Anchored at the crystal origin ``q = 0`` and nowhere else (O44). Deriving the fold positions
    from a measured or tracker-selected q would let the scene's own choice of where to look
    define where the zone centres are, and the geometry would then confirm whatever the campaign
    happened to visit.
    """
    return abs(h - round(h / period) * period) <= ZONE_CENTRE_Q_TOL


def _zone_centre_refine(report, view):
    """``structural_endpoint`` where a declared gapless branch legitimately has nothing to show.

    ``blocked_unresolved`` and ``structural_endpoint`` are different claims and the difference
    matters to what an operator does next. Blocked says *unresolved pending better data* — look
    again, longer or with finer resolution, and the answer may change. Structural endpoint says
    *the declared physics terminates the track here* — nothing is missing, and no amount of
    counting will produce a peak, because a gapless branch reaches ω → 0 at the zone centre and
    is swallowed by the elastic response.

    Four conditions, each of which must hold, and the absence of any one of them leaves the
    absence exactly as it was:

    1. a zone period is DECLARED (nothing here is derived from the data);
    2. the neighbour arena sits at a zone centre of that period;
    3. the elastic line is chosen there, so there IS an elastic response to be swallowed by;
    4. the mode's energy is within a plausible descent of zero over the ω tolerance the linkage
       grammar already uses — a 9 meV optic mode is not explained away by an acoustic branch's
       endpoint.

    What this factor does NOT do is price anything. ``presence`` keys on ``searched_no_signal``
    alone, so a report reinterpreted here simply stops carrying a miss — the
    no-penalty-at-an-endpoint behaviour is EMERGENT from the visibility grammar rather than
    special-cased into a score function, which is what keeps the exemption in one place.
    """
    sample = view.sample
    if not _zone_centres_declared(sample):
        return None
    if not report["elastic_present"]:
        return None
    fp_j = view.footprints[report["neighbour"]]
    h = path_coordinate(fp_j.q)
    if h is None or not _at_zone_centre(h, sample.zone_period):
        return None
    fp_i = view.footprints[report["scan_ref"]]
    if abs(report["omega"]) > ENDPOINT_OMEGA_TOL_FACTOR * link_tol(fp_i, fp_j):
        return None
    return Visibility.STRUCTURAL_ENDPOINT


def _zone_centre_degradations(sample) -> tuple[str, ...]:
    return () if _zone_centres_declared(sample) else ("no_declared_zone_period",)


ZONE_CENTRE_APPROACH = SceneFactor(
    # v2: the endpoint prerequisite is the ELASTIC component strictly. Under v1 a quasielastic
    # feature satisfied it, so a v1 revision may carry structural endpoints this factor no longer
    # grants — the fingerprint has to say so.
    name="zone_centre_approach", kind="structural_likelihood", version="2",
    consumes=("q", "omega", "footprint", "sample.zone_period"),
    config=(("endpoint_omega_tol_factor", ENDPOINT_OMEGA_TOL_FACTOR),
            ("link_tol_floor", LINK_TOL_FLOOR), ("zone_centre_q_tol", ZONE_CENTRE_Q_TOL)),
    # No score, therefore no bound to declare — and, because ``_score_span`` looks only at
    # SCORING factors, no pruning cost either. A visibility-grammar factor changes what the
    # scoring factors see, not what they may add.
    score_bound=None,
    refine_visibility=_zone_centre_refine, degradations=_zone_centre_degradations)


# ----------------------------------------------------------------------------
# reciprocal_lattice_localization
# ----------------------------------------------------------------------------
def _lattice_classify(view) -> dict:
    """Bragg-compatible / off-lattice / unclassified, per ELASTIC component. Belief-neutral.

    "Bragg" is a SCENE-level identity, and it has to be: the local catalogue's elastic entry is
    lattice-blind by design — it fits a resolution-limited feature at ω = 0 and knows nothing
    about where in reciprocal space it sits. So the local evidence cannot have priced lattice
    position, and classifying it here re-prices nothing (H12(a)).

    It CLASSIFIES and does not score, and that is the categorical humility the elastic channel
    demands. Elastic intensity is ubiquitous — sample environment, mounts, incoherent scattering,
    a bit of powder on the plate. An off-lattice elastic feature is therefore *not* improbable;
    it is merely not-Bragg. Attaching a likelihood to "elastic where no reflection is declared"
    would penalize a completely ordinary observation, so the scene records what the feature is
    relative to the declared lattice and leaves belief alone.

    Modes are not classified at all: a phonon at a reflection's q is a phonon, not a reflection.
    Neither are QUASIELASTIC components, and they are ABSENT from the map rather than
    ``unclassified``: a broad quasielastic response is diffusive scattering, not a candidate
    reflection that happens to lack a declaration, so asking where it sits relative to the reciprocal
    lattice is asking the wrong question. ``unclassified`` keeps its one meaning — an elastic
    component with nothing declared to compare it against.
    """
    reflections = view.sample.reflections
    out = {}
    for ref in view.scan_refs:
        for c in view.components[ref]:
            if c.kind != "elastic":
                continue
            h = path_coordinate(c.q)
            if h is None:
                # Two different reasons to have no answer, and they get two different states.
                # This one is per-COMPONENT and invisible to ``degradations(sample)``, which sees
                # the declaration and not the arenas — so an unplaceable arena under a perfectly
                # good reflection list would otherwise record "unclassified" with no reason
                # anywhere, which is exactly the silent inertness the degradation ledger exists
                # to prevent. The state names it instead.
                out[c.component_id] = "unplaceable_q"
            elif not reflections:
                # Nothing declared is not evidence of being off-lattice — the question was never
                # asked. The reason is build-level (``no_declared_reflections``).
                out[c.component_id] = "unclassified"
            elif any(abs(h - float(r)) <= BRAGG_Q_TOL for r in reflections):
                out[c.component_id] = "bragg_compatible"
            else:
                out[c.component_id] = "off_lattice"
    return dict(sorted(out.items()))


def _lattice_degradations(sample) -> tuple[str, ...]:
    return () if sample.reflections else ("no_declared_reflections",)


RECIPROCAL_LATTICE_LOCALIZATION = SceneFactor(
    # v2: quasielastic components are no longer classified at all (they were "unclassified" or even
    # "bragg_compatible" under v1), so a v1 map is not reproducible here.
    name="reciprocal_lattice_localization", kind="structural_likelihood", version="2",
    consumes=("q", "sample.reflections"),
    config=(("bragg_q_tol", BRAGG_Q_TOL),),
    # Classification only: no score, so no bound, and no effect on the builder's pruning.
    score_bound=None,
    classify=_lattice_classify, degradations=_lattice_degradations)


# The live registry: the builder falls back to it when a caller passes no explicit factor set.
FACTORS: dict[str, SceneFactor] = {f.name: f for f in (PRESENCE, CONNECTIVITY,
                                                       ZONE_CENTRE_APPROACH,
                                                       RECIPROCAL_LATTICE_LOCALIZATION)}
