"""The generic structure-hypothesis recipe type.

A :class:`Hypothesis` names an ordered set of components and knows how to build its
composite; it says nothing about *which* structures the physics admits. The DHO-specific
enumeration and admissibility gates live in :mod:`isar.analyze.phonon`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Hypothesis:
    """A named structure recipe: the ordered components that build its composite. Each entry is
    either a component *class* (instantiated with its default prefix) or a pre-built component
    *instance* (an explicit prefix override, e.g. ``PhononDHO(prefix="phonon2")`` for N-mode).
    """

    label: str
    components: tuple

    @property
    def k(self) -> int:
        """Number of free parameters (the model's degrees of freedom consumed)."""
        return self.build().n_params

    def build(self):
        """Instantiate the CompositeModel for this hypothesis. Classes are instantiated;
        pre-built instances (components are stateless) are reused as-is."""
        from isar.analyze.components import CompositeModel
        return CompositeModel([c() if isinstance(c, type) else c for c in self.components])


# Temporary P2.7 aliases — removed in P2.8. Lazy (module ``__getattr__``) rather than an
# eager bottom import because :mod:`isar.analyze.phonon` imports this module: an eager
# import would deadlock the graph when phonon is imported first.
_P27_ALIASES = {
    "elastic_admissible": "elastic_admissible",
    "quasielastic_admissible": "quasielastic_admissible",
    "antistokes_mode": "antistokes_mode",
    "enumerate_structures": "enumerate_phonon_structures",
    "enumerate_multimode": "enumerate_phonon_multimode",
}


def __getattr__(name: str):
    target = _P27_ALIASES.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from isar.analyze import phonon
    return getattr(phonon, target)
