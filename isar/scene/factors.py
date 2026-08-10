"""The scene-factor interface — the declaration, not the physics (SCENE-3 PR 3a).

A scene factor is a term ``ln φ_f(S)`` of the SCENE_DESIGN §2b score. Everything in this
module exists to make the factor ban mechanical rather than a matter of reviewer memory:

* a factor declares its probabilistic ``kind`` (``scene_prior`` or ``structural_likelihood``)
  — there is no undeclared heuristic bonus, because ``validate_factor`` refuses one;
* a factor declares the scene variables it ``consumes`` and a ``version``, and both enter the
  revision's ``registry_fingerprint``, so a scored revision names the factor set that scored
  it and cannot be silently re-scored by a different one;
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
    """

    name: str
    kind: str
    version: str
    consumes: tuple[str, ...] = ()
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


def registry_fingerprint(factors) -> str:
    """Fingerprint of the ``(name, kind, version)`` triples of ``factors``.

    Accepts the registry mapping or any iterable of factors. Order-free by construction: the
    triples are sorted, so two callers assembling the same factor set in different orders
    stamp the same revision.
    """
    values = factors.values() if isinstance(factors, dict) else factors
    return fingerprint(sorted([f.name, f.kind, f.version] for f in values))


# The live registry. EMPTY in this slice — the physics factors land next; the builder falls
# back to it when a caller passes no explicit factor set.
FACTORS: dict[str, SceneFactor] = {}
