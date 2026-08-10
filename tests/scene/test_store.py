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
from isar.scene.store import (ConcurrentSceneWrite, POINTER_SCHEMA, pointer_digest, read_current,
                              write_revision)

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


# ----------------------------------------------------------------------------
# The pointer contract, enforced before any field is used (review finding A)
# ----------------------------------------------------------------------------
def _repoint(report_dir, **fields):
    rev = scene(chain_and_distant())
    write_revision(report_dir, rev)
    pointer = json.loads((report_dir / "scene" / "current.json").read_text(encoding="utf-8"))
    pointer.update(fields)
    (report_dir / "scene" / "current.json").write_bytes(canonical_bytes(pointer))
    return rev


@pytest.mark.parametrize("fields, message", [
    ({"schema": "isar.scene-pointer/2"}, "declares schema"),
    ({"schema": None}, "declares schema"),
    # Not a digest: a path, a short hex string, upper case, a non-string. Each would be spelled
    # straight into a filename by the reader.
    ({"digest": "../../etc/passwd"}, "names no digest"),
    ({"digest": "abc123"}, "names no digest"),
    ({"digest": "A" * 64}, "names no digest"),
    ({"digest": 12}, "names no digest"),
    ({"parent": "not-a-digest"}, "malformed parent"),
    ({"written_at": None}, "no written_at"),
    ({"written_at": 17}, "no written_at"),
])
def test_a_pointer_field_that_breaks_the_contract_refuses_on_read_and_on_write(
        tmp_path, fields, message):
    rev = _repoint(tmp_path, **fields)
    with pytest.raises(ValueError, match=message):
        read_current(tmp_path)
    with pytest.raises(ValueError, match=message):
        write_revision(tmp_path, rev)


def test_a_pointer_that_is_not_an_object_raises(tmp_path):
    write_revision(tmp_path, scene(chain_and_distant()))
    (tmp_path / "scene" / "current.json").write_bytes(b'["digest"]')
    with pytest.raises(ValueError, match="is not an object"):
        read_current(tmp_path)


# ----------------------------------------------------------------------------
# Existence is not integrity (review finding B)
# ----------------------------------------------------------------------------
def test_rebuilding_repairs_a_corrupt_current_blob_before_reporting_unchanged(tmp_path):
    """A blob at the right NAME with the wrong bytes: the state a name-only check cannot see.
    ``unchanged=True`` may only mean "the content on disk is this revision", so the bytes are
    compared and repaired first."""
    rev = scene(chain_and_distant())
    blob = write_revision(tmp_path, rev).path
    before = pointer_bytes(tmp_path)
    blob.write_bytes(b'{"schema":"isar.scene-revision/1"}')

    result = write_revision(tmp_path, rev)
    assert result.unchanged is True
    assert blob.read_bytes() == rev.canonical
    assert pointer_bytes(tmp_path) == before
    assert read_current(tmp_path)[0]["digest"] == rev.digest


def test_a_corrupt_orphan_blob_is_repaired_before_the_pointer_flips_onto_it(tmp_path):
    first = scene(chain_and_distant())
    write_revision(tmp_path, first)
    moved = chain_and_distant()
    moved[1] = mk_obs("e:2:d", [cand("A", -12.25), cand("B", -12.5)], q=(1.1, 0.0, 0.0))
    second = scene(moved)
    # An interrupted publication that left a TRUNCATED orphan behind.
    (tmp_path / "scene" / f"{second.digest}.json").write_bytes(second.canonical[:10])

    result = write_revision(tmp_path, second)
    assert result.unchanged is False and result.parent == first.digest
    assert result.path.read_bytes() == second.canonical
    assert canonical_bytes(read_current(tmp_path)[1]) == second.canonical


# ----------------------------------------------------------------------------
# A second writer (review finding C)
# ----------------------------------------------------------------------------
def test_an_interleaved_writer_refuses_its_flip_and_keeps_its_blob(tmp_path):
    """``isar report --scene`` beside a live driver. A snapshots the pointer, B publishes, A's
    flip must refuse rather than dropping B's revision out of the lineage."""
    first = scene(chain_and_distant())
    write_revision(tmp_path, first)
    snapshot = pointer_digest(tmp_path)              # A snapshots its inputs here

    moved = chain_and_distant()
    moved[0] = mk_obs("e:1:d", [cand("A", -10.5), cand("B", -11.0)], q=(1.0, 0.0, 0.0))
    b_revision = scene(moved)
    write_revision(tmp_path, b_revision, source="report_cli")     # B publishes meanwhile

    moved_again = chain_and_distant()
    moved_again[1] = mk_obs("e:2:d", [cand("A", -12.25), cand("B", -12.5)], q=(1.1, 0.0, 0.0))
    a_revision = scene(moved_again)
    with pytest.raises(ConcurrentSceneWrite, match=b_revision.digest):
        write_revision(tmp_path, a_revision, expected_parent=snapshot)

    assert read_current(tmp_path)[0]["digest"] == b_revision.digest
    # A's blob is written and addressable — nothing it computed was lost, only the flip refused.
    assert (tmp_path / "scene" / f"{a_revision.digest}.json").read_bytes() == a_revision.canonical


def test_an_uncontested_expected_parent_publishes_normally(tmp_path):
    first = scene(chain_and_distant())
    write_revision(tmp_path, first)
    moved = chain_and_distant()
    moved[0] = mk_obs("e:1:d", [cand("A", -10.5), cand("B", -11.0)], q=(1.0, 0.0, 0.0))
    result = write_revision(tmp_path, scene(moved), expected_parent=pointer_digest(tmp_path))
    assert result.unchanged is False and result.parent == first.digest


def test_expecting_an_empty_store_is_not_the_same_as_expecting_nothing(tmp_path):
    """``expected_parent=None`` is a real expectation (the store was empty), not an omission."""
    assert pointer_digest(tmp_path) is None
    write_revision(tmp_path, scene(chain_and_distant()), expected_parent=None)
    moved = chain_and_distant()
    moved[0] = mk_obs("e:1:d", [cand("A", -10.5), cand("B", -11.0)], q=(1.0, 0.0, 0.0))
    with pytest.raises(ConcurrentSceneWrite):
        write_revision(tmp_path, scene(moved), expected_parent=None)


def test_an_unchanged_rebuild_is_not_refused_by_a_stale_expectation(tmp_path):
    """The pointer already names this revision, so there is no flip to lose — whoever published
    it, the state on disk is the state this writer wanted."""
    rev = scene(chain_and_distant())
    write_revision(tmp_path, rev)
    assert write_revision(tmp_path, rev, expected_parent=None).unchanged is True


# ----------------------------------------------------------------------------
# Lineage is durable (review finding D)
# ----------------------------------------------------------------------------
def history(report_dir):
    path = report_dir / "scene" / "history.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_every_flip_appends_a_lineage_line_the_pointer_cannot_remember(tmp_path):
    """A→B→C: ``current.json`` holds only C→B, so the B→A link exists nowhere else."""
    a = scene(chain_and_distant())
    write_revision(tmp_path, a)
    moved = chain_and_distant()
    moved[0] = mk_obs("e:1:d", [cand("A", -10.5), cand("B", -11.0)], q=(1.0, 0.0, 0.0))
    b = scene(moved)
    write_revision(tmp_path, b, source="report_cli")
    moved[1] = mk_obs("e:2:d", [cand("A", -12.25), cand("B", -12.5)], q=(1.1, 0.0, 0.0))
    c = scene(moved)
    write_revision(tmp_path, c)

    lines = history(tmp_path)
    assert [(e["digest"], e["parent"]) for e in lines] == [
        (a.digest, None), (b.digest, a.digest), (c.digest, b.digest)]
    assert [e["source"] for e in lines] == ["driver", "report_cli", "driver"]
    assert all(e["written_at"].endswith("+00:00") for e in lines)


def test_an_unchanged_rebuild_appends_no_history(tmp_path):
    rev = scene(chain_and_distant())
    write_revision(tmp_path, rev)
    write_revision(tmp_path, rev)
    assert len(history(tmp_path)) == 1
