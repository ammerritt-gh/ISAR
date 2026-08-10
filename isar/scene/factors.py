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

The registry is EMPTY here on purpose: real physics factors land in the next task, and tests
inject neutral ones. ``RESERVED_FACTOR_NAMES`` reserves the names those factors will take so
two slices cannot pick the same name for different terms.

Stdlib-only, like ``model.py`` — see ``tests/scene/test_import_ban.py``.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from .model import fingerprint

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


# The live registry. EMPTY in this slice — the physics factors land next; the builder falls
# back to it when a caller passes no explicit factor set.
FACTORS: dict[str, SceneFactor] = {}
