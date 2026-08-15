"""The one family↔generator vocabulary and the discovery generator registry (PR 4.2b B1).

Stdlib-only, like every ``isar.schema`` module: ``isar.scene`` (agenda derivation) and
``isar.drive`` (controller, inventory) both read this vocabulary, and neither may import the
other — the shared words have to live below both. Before this module existed the family
bindings lived in ``isar.scene.agenda.GENERATOR_REFS`` and the registry machinery in
``isar.drive.acquisition.inventory``; each keeps a re-export so its callers are unchanged, but
there is exactly one declaration of each name now.

Two registries exist side by side, deliberately (until SCENE-6):

* the LEGACY registry (``isar.drive.acquisition.inventory.GENERATOR_REGISTRY``) — the
  compatibility adapter around the classical selector; its import-time dispatch check pins the
  legacy generator set exactly, and this module does not touch it;
* :data:`DISCOVERY_GENERATOR_REGISTRY` — the seven question-bound discovery generators, one
  spec per generator ID in :data:`FAMILY_GENERATORS`.

``registry_fingerprint`` here takes the registry EXPLICITLY — no default. The old default
bound the drive-owned legacy registry, which this module cannot import (schema sits below
drive); more importantly, a fingerprint whose subject is implicit is how two registries end up
sharing one "version" by accident.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .action_identity import canonical_json


@dataclass(frozen=True)
class GeneratorSpec:
    """One registered action generator: the declarative unit an inventory is validated against."""

    generator_id: str
    version: int = 1


#: Family → generator IDs, the SCENE question families' action producers (H19: seven IDs,
#: every family at least one). ``exposure_fill`` exists because the opening book's
#: underexposed_cell proposals serve VerifyCompleteness's exposure floor and only that
#: closure — exhaustion is proved against ``spec.generator_ids`` exactly, so exposure work
#: needs its own generator identity or it would be unattributable.
FAMILY_GENERATORS: dict[str, tuple[str, ...]] = {
    "DiscoverRegion": ("opening_book_survey",),
    "ConfirmPresence": ("presence_probe",),
    "ClassifyIdentity": ("identity_discriminator",),
    "ConnectTrack": ("track_bridge_scan",),
    "EstimateParameter": ("parameter_refine",),
    "TestModelMismatch": ("mismatch_probe",),
    "VerifyCompleteness": ("exposure_fill",),
}

#: The seven discovery generators, ordered as their families are declared above.
DISCOVERY_GENERATOR_REGISTRY: tuple[GeneratorSpec, ...] = tuple(
    GeneratorSpec(generator_id)
    for family in FAMILY_GENERATORS
    for generator_id in FAMILY_GENERATORS[family])


def registry_fingerprint(registry: tuple[GeneratorSpec, ...]) -> str:
    """Derive a registry version from the ordered generator ids + versions.

    Deriving rather than hand-maintaining a string means a generator added, removed, renamed,
    or revised cannot silently keep an old terminal's proof looking current. The registry is a
    REQUIRED argument: the caller always names whose fingerprint it wants.
    """
    payload = canonical_json([[spec.generator_id, spec.version] for spec in registry])
    return "isar.generator-registry/1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def registry_manifest(registry: tuple[GeneratorSpec, ...]) -> list[list]:
    """The embeddable ``[[generator_id, version], ...]`` manifest a fingerprint digests —
    what an inventory or a DecisionBasis carries so the fingerprint stays auditable."""
    return [[spec.generator_id, spec.version] for spec in registry]


DISCOVERY_GENERATOR_REGISTRY_VERSION = registry_fingerprint(DISCOVERY_GENERATOR_REGISTRY)
