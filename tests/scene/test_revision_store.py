"""The generic RevisionStore core (SCENE-4 PR 4.1, slice 1).

``tests/scene/test_store.py`` is the scene binding's contract and passes UNMODIFIED — that is
the refactor's acceptance gate. This file proves only what the generalization added: a second
binding gets its own directory, its own pointer schema enforced by name, diagnostics in its own
artifact vocabulary, and full coexistence with the scene binding in one report directory. The
duck-typed revision protocol (``.canonical``/``.digest``) is exercised with a hand-built object,
no scene builder involved.
"""
from dataclasses import dataclass

import pytest

from isar.scene.model import canonical_bytes, digest_bytes
from isar.scene.store import ConcurrentRevisionWrite, RevisionStore, SCENE_STORE


@dataclass(frozen=True)
class FakeRevision:
    canonical: bytes
    digest: str


def rev(payload: dict) -> FakeRevision:
    blob = canonical_bytes(payload)
    return FakeRevision(canonical=blob, digest=digest_bytes(blob))


STORE = RevisionStore("agenda", "isar.agenda-pointer/1", "agenda")


def test_a_second_binding_round_trips_in_its_own_directory(tmp_path):
    revision = rev({"schema": "isar.agenda-revision/1", "specs": []})
    result = STORE.write_revision(tmp_path, revision)
    assert result.path == tmp_path / "agenda" / f"{revision.digest}.json"
    pointer, payload = STORE.read_current(tmp_path)
    assert pointer["schema"] == "isar.agenda-pointer/1"
    assert canonical_bytes(payload) == revision.canonical
    assert STORE.write_revision(tmp_path, revision).unchanged is True


def test_bindings_coexist_and_never_read_each_other(tmp_path):
    agenda = rev({"schema": "isar.agenda-revision/1", "specs": []})
    STORE.write_revision(tmp_path, agenda)
    # The scene binding sees an empty scene store, not the agenda pointer.
    assert SCENE_STORE.read_current(tmp_path) is None
    assert SCENE_STORE.pointer_digest(tmp_path) is None
    assert STORE.pointer_digest(tmp_path) == agenda.digest


def test_a_foreign_pointer_schema_refuses_by_name(tmp_path):
    """An agenda pointer is not a scene pointer even at identical shape (H1: no guessing)."""
    agenda = rev({"schema": "isar.agenda-revision/1", "specs": []})
    STORE.write_revision(tmp_path, agenda)
    scene_view_of_agenda = RevisionStore("agenda", "isar.scene-pointer/1", "scene")
    with pytest.raises(ValueError, match="declares schema 'isar.agenda-pointer/1'"):
        scene_view_of_agenda.read_current(tmp_path)


def test_diagnostics_wear_the_binding_label(tmp_path):
    revision = rev({"specs": []})
    STORE.write_revision(tmp_path, revision)
    (tmp_path / "agenda" / "current.json").write_bytes(b"{not json")
    with pytest.raises(ValueError, match="unreadable agenda pointer"):
        STORE.read_current(tmp_path)


def test_concurrency_refusal_through_a_second_binding(tmp_path):
    first = rev({"specs": ["a"]})
    STORE.write_revision(tmp_path, first)
    snapshot = STORE.pointer_digest(tmp_path)
    STORE.write_revision(tmp_path, rev({"specs": ["b"]}))
    with pytest.raises(ConcurrentRevisionWrite, match="agenda pointer moved"):
        STORE.write_revision(tmp_path, rev({"specs": ["c"]}), expected_parent=snapshot)
