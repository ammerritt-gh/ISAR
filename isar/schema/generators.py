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


@dataclass(frozen=True)
class QuestionActionAssociation:
    """Why one physical action serves one agenda question (PR 4.2b B2).

    Provenance is many-to-many and lives BESIDE the intent, never on it: an intent's
    scientific identity is what it measures, and stapling a question ID onto it would make
    the same physical measurement two different actions for two askers — exactly what dedup
    exists to prevent. The question reference is the PASS-1 basis agenda digest plus the
    question ID: pass-1 is the exhaustion-free derivation whose digest exists before
    generators run (the final agenda digest does not exist yet while actions are being
    generated), the same comparand ``QuestionExhaustion`` freshness binds.

    ``family_priority`` and ``dependency_depth`` are the association's rank inputs; the
    scheduler aggregates by MINIMUM over an evaluation's associations, order-invariantly.
    """

    basis_agenda_digest: str
    question_id: str
    generator: str
    family_priority: int
    dependency_depth: int
    reason: str

    def __post_init__(self) -> None:
        for name in ("basis_agenda_digest", "question_id", "generator", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"QuestionActionAssociation.{name} must be a non-empty string")
        for name in ("family_priority", "dependency_depth"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"QuestionActionAssociation.{name} must be a non-negative integer, "
                    f"got {value!r}")

    @property
    def rank_key(self) -> tuple:
        """The association's lexicographic rank contribution; ties break on identity fields
        so aggregation is deterministic regardless of iteration order."""
        return (self.family_priority, self.dependency_depth, self.question_id, self.generator)

    def to_dict(self) -> dict:
        return {"basis_agenda_digest": self.basis_agenda_digest,
                "question_id": self.question_id,
                "generator": self.generator,
                "family_priority": self.family_priority,
                "dependency_depth": self.dependency_depth,
                "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict) -> "QuestionActionAssociation":
        return cls(basis_agenda_digest=str(d["basis_agenda_digest"]),
                   question_id=str(d["question_id"]),
                   generator=str(d["generator"]),
                   family_priority=int(d["family_priority"]),
                   dependency_depth=int(d["dependency_depth"]),
                   reason=str(d["reason"]))


def union_associations(*groups) -> tuple[QuestionActionAssociation, ...]:
    """The canonically ordered union of association groups — what dedup stamps onto a
    surviving evaluation. Deduplicates exact duplicates and sorts by ``rank_key`` then the
    remaining fields, so the result is identical under any grouping or iteration order."""
    seen: dict[tuple, QuestionActionAssociation] = {}
    for group in groups:
        for association in group:
            key = (association.basis_agenda_digest, association.question_id,
                   association.generator, association.family_priority,
                   association.dependency_depth, association.reason)
            seen.setdefault(key, association)
    return tuple(sorted(seen.values(),
                        key=lambda a: (a.rank_key, a.basis_agenda_digest, a.reason)))


def aggregate_rank(associations) -> tuple[tuple, QuestionActionAssociation]:
    """The MINIMUM lexicographic rank over an evaluation's associations, plus the
    association that supplied it (persisted with the decision so the winning attribution is
    auditable). Order-invariant: ``rank_key`` embeds the identity tiebreaks."""
    pool = list(associations)
    if not pool:
        raise ValueError("aggregate_rank needs at least one association; a discovery "
                         "evaluation with none is unattributable by construction")
    supplier = min(pool, key=lambda a: a.rank_key)
    return supplier.rank_key, supplier


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
