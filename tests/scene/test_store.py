"""The scene revision store: replay, idempotence, crash safety, loud corruption (SCENE-3 PR 3c).

Observations are hand-built — no fitter, no workspace, no campaign — so every failure here is a
failure of the store contract itself: identical inputs replay to an identical blob and an unmoved
pointer, a changed scene appends without disturbing what came before, an interrupted write leaves
the previous revision readable, and anything that cannot be vouched for raises instead of being
handed back.
"""
import json
from dataclasses import replace

import pytest

from isar.scene.build import build_scene
from isar.scene.model import canonical_bytes
from isar.scene.store import POINTER_SCHEMA, read_current, write_revision

from .test_build import COUPLER, ZERO, cand, chain_and_distant, mk_obs

FACTORS = [COUPLER, ZERO]


def scene(observations):
    return build_scene(observations, factors=FACTORS)


def pointer_bytes(report_dir):
    return (report_dir / "scene" / "current.json").read_bytes()


def blobs(report_dir):
    return sorted(p.name for p in (report_dir / "scene").glob("*.json")
                  if p.name != "current.json")


# ----------------------------------------------------------------------------
# Round trip
# ----------------------------------------------------------------------------
def test_absent_workspace_and_absent_pointer_read_as_none(tmp_path):
    """Absence is normal and silent; only corruption is loud."""
    assert read_current(tmp_path / "report") is None
    (tmp_path / "report" / "scene").mkdir(parents=True)
    assert read_current(tmp_path / "report") is None


def test_write_then_read_returns_the_payload_byte_for_byte(tmp_path):
    rev = scene(chain_and_distant())
    result = write_revision(tmp_path, rev)
    assert result.unchanged is False and result.parent is None
    assert result.digest == rev.digest and result.path.name == f"{rev.digest}.json"
    assert result.path.read_bytes() == rev.canonical

    pointer, payload = read_current(tmp_path)
    assert pointer["schema"] == POINTER_SCHEMA
    assert pointer["digest"] == rev.digest and pointer["parent"] is None
    assert pointer["written_at"].endswith("+00:00")
    assert canonical_bytes(payload) == rev.canonical


def test_the_digest_covers_no_timestamp_and_no_lineage(tmp_path):
    """Time and parentage live in the pointer precisely so a rebuild replays to one digest."""
    rev = scene(chain_and_distant())
    write_revision(tmp_path, rev)
    pointer, payload = read_current(tmp_path)
    assert {"written_at", "parent"}.isdisjoint(payload)
    assert set(pointer) == {"schema", "digest", "parent", "written_at"}


# ----------------------------------------------------------------------------
# Replay
# ----------------------------------------------------------------------------
def test_rebuilding_an_unchanged_scene_writes_nothing(tmp_path):
    obs = chain_and_distant()
    write_revision(tmp_path, scene(obs))
    before = pointer_bytes(tmp_path)

    again = write_revision(tmp_path, scene(obs))
    assert again.unchanged is True
    assert pointer_bytes(tmp_path) == before      # not even a refreshed written_at
    assert len(blobs(tmp_path)) == 1


def test_permuted_inputs_replay_to_the_same_revision(tmp_path):
    """Mirrors the builder's own order-invariance check, through the store: retyping a card in
    another order must not mint a revision."""
    obs = chain_and_distant()
    permuted = [replace(o, candidates=tuple(reversed(o.candidates))) for o in reversed(obs)]
    write_revision(tmp_path, scene(obs))
    assert write_revision(tmp_path, build_scene(permuted, factors=[ZERO, COUPLER])).unchanged


def test_a_changed_scene_appends_and_records_its_parent(tmp_path):
    first = scene(chain_and_distant())
    write_revision(tmp_path, first)

    moved = chain_and_distant()
    moved[0] = mk_obs("e:1:d", [cand("A", -10.5), cand("B", -11.0)], q=(1.0, 0.0, 0.0))
    second = write_revision(tmp_path, scene(moved))

    assert second.digest != first.digest and second.unchanged is False
    assert second.parent == first.digest
    pointer, payload = read_current(tmp_path)
    assert pointer["digest"] == second.digest and pointer["parent"] == first.digest
    # Nothing is ever deleted: the superseded revision stays addressable by its digest.
    assert sorted(blobs(tmp_path)) == sorted([f"{first.digest}.json", f"{second.digest}.json"])
    assert (tmp_path / "scene" / f"{first.digest}.json").read_bytes() == first.canonical


# ----------------------------------------------------------------------------
# Crash window
# ----------------------------------------------------------------------------
def test_a_blob_written_without_its_pointer_leaves_the_old_revision_current(tmp_path):
    """The unsafe half of blob-first: an orphaned new blob. It is safe — the old pointer still
    names a whole revision, and the next write simply re-uses the blob already on disk."""
    first = scene(chain_and_distant())
    write_revision(tmp_path, first)

    moved = chain_and_distant()
    moved[1] = mk_obs("e:2:d", [cand("A", -12.25), cand("B", -12.5)], q=(1.1, 0.0, 0.0))
    orphan = scene(moved)
    (tmp_path / "scene" / f"{orphan.digest}.json").write_bytes(orphan.canonical)

    pointer, payload = read_current(tmp_path)
    assert pointer["digest"] == first.digest
    assert canonical_bytes(payload) == first.canonical

    # Completing the interrupted publication flips the pointer onto the blob already there.
    result = write_revision(tmp_path, orphan)
    assert result.unchanged is False and result.parent == first.digest
    assert read_current(tmp_path)[0]["digest"] == orphan.digest


def test_rewriting_a_revision_whose_blob_was_lost_restores_it(tmp_path):
    """The state the unchanged short-circuit must not skip past: pointer intact, blob gone.
    Short-circuiting on the pointer alone would report a cheerful ``unchanged`` at a workspace
    that ``read_current`` can only raise on — the one corruption a rebuild cannot heal."""
    rev = scene(chain_and_distant())
    blob = write_revision(tmp_path, rev).path
    before = pointer_bytes(tmp_path)
    blob.unlink()

    result = write_revision(tmp_path, rev)
    assert result.unchanged is True                  # honest: the content on disk IS this one
    assert blob.read_bytes() == rev.canonical        # ...because it was healed first
    assert pointer_bytes(tmp_path) == before         # and the pointer never moved
    assert read_current(tmp_path)[0]["digest"] == rev.digest


def test_a_lost_pointer_restarts_lineage_over_the_surviving_blobs(tmp_path):
    """Accepted by design: the pointer is convenience, never authority. Lineage restarts, the
    superseded revisions stay addressable, and nothing that carries evidence is lost."""
    first = scene(chain_and_distant())
    write_revision(tmp_path, first)
    moved = chain_and_distant()
    moved[0] = mk_obs("e:1:d", [cand("A", -10.5), cand("B", -11.0)], q=(1.0, 0.0, 0.0))
    second = scene(moved)
    write_revision(tmp_path, second)

    (tmp_path / "scene" / "current.json").unlink()
    again = write_revision(tmp_path, second)
    assert again.unchanged is False and again.parent is None
    assert sorted(blobs(tmp_path)) == sorted([f"{first.digest}.json", f"{second.digest}.json"])


# ----------------------------------------------------------------------------
# Corruption is loud
# ----------------------------------------------------------------------------
def test_a_pointer_naming_a_missing_revision_raises(tmp_path):
    rev = scene(chain_and_distant())
    write_revision(tmp_path, rev)
    (tmp_path / "scene" / f"{rev.digest}.json").unlink()
    with pytest.raises(ValueError, match=rev.digest):
        read_current(tmp_path)


def test_a_truncated_revision_raises(tmp_path):
    rev = scene(chain_and_distant())
    blob = write_revision(tmp_path, rev).path
    blob.write_bytes(rev.canonical[:len(rev.canonical) // 2])
    with pytest.raises(ValueError, match="unreadable scene revision"):
        read_current(tmp_path)


def test_a_hand_edited_revision_raises_on_the_digest(tmp_path):
    """Parseable, plausible, and not what was published — the case a byte-count check misses."""
    rev = scene(chain_and_distant())
    blob = write_revision(tmp_path, rev).path
    payload = json.loads(blob.read_bytes())
    payload["clusters"][0]["alternatives"][0]["posterior"] = 0.99
    blob.write_bytes(canonical_bytes(payload))
    with pytest.raises(ValueError, match="does not match its digest"):
        read_current(tmp_path)


def test_a_corrupt_pointer_raises(tmp_path):
    write_revision(tmp_path, scene(chain_and_distant()))
    (tmp_path / "scene" / "current.json").write_bytes(b"{not json")
    with pytest.raises(ValueError, match="unreadable scene pointer"):
        read_current(tmp_path)


def test_writing_over_a_corrupt_pointer_raises_rather_than_losing_lineage(tmp_path):
    """The write path reads the pointer first, so corruption is loud there too — a driver hook
    must expect ValueError from ``write_revision``, not only from ``read_current``."""
    rev = scene(chain_and_distant())
    write_revision(tmp_path, rev)
    (tmp_path / "scene" / "current.json").write_bytes(b"{not json")
    with pytest.raises(ValueError, match="unreadable scene pointer"):
        write_revision(tmp_path, rev)


def test_a_pointer_without_a_digest_raises(tmp_path):
    write_revision(tmp_path, scene(chain_and_distant()))
    (tmp_path / "scene" / "current.json").write_bytes(b'{"schema":"isar.scene-pointer/1"}')
    with pytest.raises(ValueError, match="names no digest"):
        read_current(tmp_path)
