"""The shared family↔generator vocabulary and discovery registry (PR 4.2b B1).

One declaration of each name, below both consumers: ``isar.scene.agenda`` re-exports
``FAMILY_GENERATORS`` as ``GENERATOR_REFS`` and ``isar.drive.acquisition.inventory``
re-exports the spec/fingerprint machinery — these tests pin that neither re-export drifts
into a second implementation, and that the vocabulary itself keeps the H19 shape.
"""
from __future__ import annotations

import inspect

import pytest

from isar.schema.generators import (
    DISCOVERY_GENERATOR_REGISTRY,
    DISCOVERY_GENERATOR_REGISTRY_VERSION,
    FAMILY_GENERATORS,
    GeneratorSpec,
    registry_fingerprint,
    registry_manifest,
)

SEVEN = ("opening_book_survey", "presence_probe", "identity_discriminator",
         "track_bridge_scan", "parameter_refine", "mismatch_probe", "exposure_fill")


def test_every_family_carries_at_least_one_generator():
    """H19: no family is generator-less — a ready question with an empty requirement set can
    never honestly exhaust (``QuestionExhaustion.exhausted()`` is False on empty), so the
    shipped vocabulary must make that state unreachable."""
    assert FAMILY_GENERATORS  # seven families, none silently dropped
    assert len(FAMILY_GENERATORS) == 7
    for family, generator_ids in FAMILY_GENERATORS.items():
        assert generator_ids, f"{family} declares no generator"


def test_seven_distinct_generator_ids_and_registry_matches():
    declared = [gid for ids in FAMILY_GENERATORS.values() for gid in ids]
    assert sorted(declared) == sorted(SEVEN)
    assert len(set(declared)) == 7
    assert tuple(spec.generator_id for spec in DISCOVERY_GENERATOR_REGISTRY) == tuple(declared)
    assert all(spec.version == 1 for spec in DISCOVERY_GENERATOR_REGISTRY)


def test_registry_fingerprint_requires_its_subject():
    """No default registry: an implicit fingerprint subject is how two registries end up
    sharing one version. (The drive-side wrapper reintroduces the LEGACY default for its own
    callers, deliberately — that is the wrapper's contract, not this function's.)"""
    parameter = inspect.signature(registry_fingerprint).parameters["registry"]
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        registry_fingerprint()  # noqa — the refusal under test


def test_fingerprint_is_deterministic_and_content_sensitive():
    assert registry_fingerprint(DISCOVERY_GENERATOR_REGISTRY) == \
        DISCOVERY_GENERATOR_REGISTRY_VERSION
    assert registry_fingerprint((GeneratorSpec("a"),)) != registry_fingerprint(
        (GeneratorSpec("b"),))
    assert registry_fingerprint((GeneratorSpec("a", 1),)) != registry_fingerprint(
        (GeneratorSpec("a", 2),))
    assert registry_manifest(DISCOVERY_GENERATOR_REGISTRY) == \
        [[gid, 1] for gid in SEVEN]


def test_drive_wrapper_and_agenda_reexport_are_the_same_declaration():
    """The relocations are re-exports, not copies: same objects, same hash for the same
    registry, and the agenda's historical name still serves the identical mapping."""
    from isar.drive.acquisition import inventory as drive_inventory
    from isar.scene import agenda

    assert agenda.GENERATOR_REFS is FAMILY_GENERATORS
    assert drive_inventory.GeneratorSpec is GeneratorSpec
    assert drive_inventory.registry_fingerprint(drive_inventory.GENERATOR_REGISTRY) == \
        registry_fingerprint(drive_inventory.GENERATOR_REGISTRY)
    # The legacy default is preserved on the drive side: same payload, same hash.
    assert drive_inventory.registry_fingerprint() == drive_inventory.GENERATOR_REGISTRY_VERSION
