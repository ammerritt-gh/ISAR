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
    QuestionActionAssociation,
    registry_fingerprint,
    registry_manifest,
    union_associations,
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


# ----------------------------------------------------------------------------
# canonical (SCENE-5 5.2, D4): omitted-when-True, old dicts read canonical, union is an OR
# ----------------------------------------------------------------------------
def _assoc(*, reason="r", canonical=True, question_id="q1"):
    return QuestionActionAssociation(
        basis_agenda_digest="a" * 64, question_id=question_id, generator="g", family_priority=0,
        dependency_depth=0, reason=reason, canonical=canonical)


def test_canonical_defaults_true_and_is_omitted_from_to_dict_only_when_true():
    canonical = _assoc(canonical=True)
    assert canonical.canonical is True
    assert "canonical" not in canonical.to_dict()

    non_canonical = _assoc(canonical=False)
    assert non_canonical.to_dict()["canonical"] is False


def test_from_dict_reads_an_old_dict_with_no_canonical_key_as_canonical():
    old_dict = _assoc().to_dict()
    assert "canonical" not in old_dict          # the pre-5.2 byte shape
    reread = QuestionActionAssociation.from_dict(old_dict)
    assert reread.canonical is True


def test_round_trip_preserves_a_non_canonical_association():
    non_canonical = _assoc(canonical=False)
    reread = QuestionActionAssociation.from_dict(non_canonical.to_dict())
    assert reread.canonical is False


def test_canonical_rejects_a_non_bool():
    with pytest.raises(ValueError):
        QuestionActionAssociation(
            basis_agenda_digest="a" * 64, question_id="q1", generator="g", family_priority=0,
            dependency_depth=0, reason="r", canonical=1)          # noqa: not a bool


def test_union_associations_merges_a_canonical_collision_to_true_both_orders():
    """Two associations colliding on the seven-field key but differing in ``canonical`` merge
    to ``canonical=True`` — arrival-order-independent."""
    canonical = _assoc(canonical=True)
    non_canonical = _assoc(canonical=False)
    assert canonical.rank_key == non_canonical.rank_key      # same seven-field key

    forward = union_associations((non_canonical,), (canonical,))
    backward = union_associations((canonical,), (non_canonical,))
    assert len(forward) == len(backward) == 1
    assert forward[0].canonical is True
    assert backward[0].canonical is True
    assert forward == backward


def test_union_associations_two_non_canonical_copies_stay_non_canonical():
    a = _assoc(canonical=False)
    b = _assoc(canonical=False)
    merged = union_associations((a,), (b,))
    assert len(merged) == 1
    assert merged[0].canonical is False


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
