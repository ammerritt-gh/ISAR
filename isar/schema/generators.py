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

    ``family_priority``, ``dependency_depth`` and ``placement_rank`` are the association's rank
    inputs; the scheduler aggregates by MINIMUM over an evaluation's associations,
    order-invariantly.

    ``placement_rank`` (review finding, 2026-08-18) is the generator's OWN ordering of the
    proposals it emitted for one question — 0 is the one it would have measured first. Without it
    every proposal a generator emits for a question ties on the two fields above, locality is 0.0
    for every action (the live runner supplies no coordinate), and the winner falls through to the
    ``scientific_action_fingerprint`` tiebreak — a SHA-256 over a payload containing the scene
    digest. The physics the opening book computes (zone-centre anchoring, placement-informed
    order) was therefore discarded, and worse: an untrusted delivery moves the scene without
    adding coverage, so the hash order reshuffled and the retry landed on a different q. The
    broken-scan bound counts one physical action, so it could not bind against a selector that
    kept wandering. Defaults to 0, which is order-preserving for any generator that emits one
    proposal per question or expresses no preference.
    """

    basis_agenda_digest: str
    question_id: str
    generator: str
    family_priority: int
    dependency_depth: int
    reason: str
    placement_rank: int = 0

    def __post_init__(self) -> None:
        for name in ("basis_agenda_digest", "question_id", "generator", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"QuestionActionAssociation.{name} must be a non-empty string")
        for name in ("family_priority", "dependency_depth", "placement_rank"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"QuestionActionAssociation.{name} must be a non-negative integer, "
                    f"got {value!r}")

    @property
    def rank_key(self) -> tuple:
        """The association's lexicographic rank contribution; ties break on identity fields
        so aggregation is deterministic regardless of iteration order."""
        return (self.family_priority, self.dependency_depth, self.placement_rank,
                self.question_id, self.generator)

    def to_dict(self) -> dict:
        return {"basis_agenda_digest": self.basis_agenda_digest,
                "question_id": self.question_id,
                "generator": self.generator,
                "family_priority": self.family_priority,
                "dependency_depth": self.dependency_depth,
                "placement_rank": self.placement_rank,
                "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict) -> "QuestionActionAssociation":
        return cls(basis_agenda_digest=str(d["basis_agenda_digest"]),
                   question_id=str(d["question_id"]),
                   generator=str(d["generator"]),
                   family_priority=int(d["family_priority"]),
                   dependency_depth=int(d["dependency_depth"]),
                   placement_rank=int(d.get("placement_rank", 0)),
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
                   association.dependency_depth, association.placement_rank,
                   association.reason)
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


#: The DURABLE subset of ``isar.scene.questions.BLOCKER_CLASS_BY_NAME`` (PR 4.2b C6). Duplicated
#: rather than imported: schema sits below scene (this module's own docstring), so
#: ``isar.schema`` may never import ``isar.scene`` — the equality with the scene-side mapping is
#: pinned by a cross-layer test instead (same posture as ``scene_digest`` vs the scene store
#: digest, and ``program_fingerprint`` vs the program store digest, elsewhere in this PR: never
#: assumed, always checked). A transient or model-authority blocker can never be closure fuel,
#: so :class:`QuestionBlockerEvidence` refuses any name outside this set at construction.
#:
#: OWNER RULING (2026-08-18, SCENE-4.3 D5): exactly TWO names are legitimate durable blockers —
#: the machine literally cannot perform the measurement, and the measurement lies outside the
#: assigned search region. ``resolution_floor`` and ``bose_dead`` were DELETED (H1, no compat
#: shim): there is no "Bose dead" — anti-Stokes only gets weaker, a matter of degree, never
#: absolute — and both belong to EIG worthwhileness pricing (SCENE-5), where a threshold says
#: "not worth measuring here", never "cannot". The ``bose_dead`` DETECTABILITY/visibility reason
#: in the scene scorer (``isar.scene.model``/``isar.scene.factors``) is a DIFFERENT vocabulary
#: and survives untouched.
DURABLE_BLOCKER_NAMES = frozenset({
    "instrument_geometry_infeasible", "program_boundary",
})


@dataclass(frozen=True)
class QuestionBlockerEvidence:
    """Durable-blocker support for a structural/saturation rejection reaching exhaustion
    (PR 4.2b C6). Production emission is 4.3 — in 4.2b only synthetic tests construct this.

    The question reference is ``(basis_agenda_digest, question_id)`` — the same PASS-1 basis
    digest :class:`QuestionActionAssociation` binds against. ``scene_digest`` and
    ``generator_registry_fingerprint`` are the freshness comparands the exhaustion producer
    checks before trusting this evidence for the CURRENT derivation, never assumed still valid.
    ``supporting_generator_ids`` names which of the question's generators this blocker backs —
    evidence naming no generator the question actually declared supports nothing.
    """

    basis_agenda_digest: str
    question_id: str
    scene_digest: str
    generator_registry_fingerprint: str
    blocker_name: str
    supporting_generator_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        for name in ("basis_agenda_digest", "question_id", "scene_digest",
                    "generator_registry_fingerprint", "blocker_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"QuestionBlockerEvidence.{name} must be a non-empty string")
        if self.blocker_name not in DURABLE_BLOCKER_NAMES:
            raise ValueError(
                f"QuestionBlockerEvidence.blocker_name {self.blocker_name!r} is not a durable "
                f"blocker (known durable names: {sorted(DURABLE_BLOCKER_NAMES)}) — a transient "
                "or model-authority blocker can never be closure fuel")
        normalized = tuple(sorted(str(g) for g in self.supporting_generator_ids))
        if any(not g.strip() for g in normalized):
            raise ValueError(
                "QuestionBlockerEvidence.supporting_generator_ids must contain non-empty "
                "generator ids — an empty id could intersect a malformed required set and "
                "back a proof nothing actually supports")
        object.__setattr__(self, "supporting_generator_ids", normalized)
        object.__setattr__(self, "reason_codes", tuple(str(r) for r in self.reason_codes))

    def to_dict(self) -> dict:
        return {"basis_agenda_digest": self.basis_agenda_digest, "question_id": self.question_id,
                "scene_digest": self.scene_digest,
                "generator_registry_fingerprint": self.generator_registry_fingerprint,
                "blocker_name": self.blocker_name,
                "supporting_generator_ids": list(self.supporting_generator_ids),
                "reason_codes": list(self.reason_codes), "detail": self.detail}

    @classmethod
    def from_dict(cls, d: dict) -> "QuestionBlockerEvidence":
        return cls(basis_agenda_digest=str(d["basis_agenda_digest"]),
                   question_id=str(d["question_id"]), scene_digest=str(d["scene_digest"]),
                   generator_registry_fingerprint=str(d["generator_registry_fingerprint"]),
                   blocker_name=str(d["blocker_name"]),
                   supporting_generator_ids=tuple(d.get("supporting_generator_ids") or ()),
                   reason_codes=tuple(d.get("reason_codes") or ()),
                   detail=str(d.get("detail", "")))


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
