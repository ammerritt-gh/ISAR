"""The generic structure-hypothesis recipe type.

A :class:`Hypothesis` names an ordered set of components and knows how to build its
composite; it says nothing about *which* structures the physics admits. The DHO-specific
enumeration and admissibility gates live in :mod:`isar.analyze.phonon`.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Hypothesis:
    """A named structure recipe: the ordered components that build its composite. Each entry is
    either a component *class* (instantiated with its default prefix) or a pre-built component
    *instance* (an explicit prefix override, e.g. ``PhononDHO(prefix="phonon2")`` for N-mode).
    """

    label: str
    components: tuple
    # The DERIVED context this candidate is defined under, or None for the ordinary case where the
    # candidate is fitted under whatever context the caller supplies. A catalogue constrained
    # variant (SCENE-1 PR-c) carries the context whose ``constraints`` hold its pins, so every
    # layer that rebuilds the problem from (hypothesis, context) — evidence rungs, prior
    # resolution, survivor bootstraps — reconstructs the SAME reduced parameter space that was
    # fitted, instead of silently re-freeing the pinned parameter. Excluded from equality: it
    # holds numpy arrays, and the label already distinguishes the candidate.
    context: object | None = field(default=None, compare=False, repr=False)

    @property
    def k(self) -> int:
        """Number of free parameters (the model's degrees of freedom consumed)."""
        return self.build().n_params

    def build(self):
        """Instantiate the CompositeModel for this hypothesis. Classes are instantiated;
        pre-built instances (components are stateless) are reused as-is."""
        from isar.analyze.components import CompositeModel
        return CompositeModel([c() if isinstance(c, type) else c for c in self.components])
