"""SCENE_DESIGN.md's generator contract matrix (PR 4.2b C9) names exactly the generator ids the
code declares — a doc claim pinned to code, never assumed to stay in sync.
"""
from __future__ import annotations

from pathlib import Path

from isar.schema.generators import FAMILY_GENERATORS

DOC = Path(__file__).parents[2] / "docs" / "SCENE_DESIGN.md"


def test_generator_matrix_names_every_declared_generator_id():
    declared = {generator_id for ids in FAMILY_GENERATORS.values() for generator_id in ids}
    assert len(declared) == 7, "FAMILY_GENERATORS no longer declares seven generators — H19(a)"
    text = DOC.read_text(encoding="utf-8")
    missing = sorted(gid for gid in declared if f"`{gid}`" not in text)
    assert not missing, (
        f"SCENE_DESIGN.md's generator matrix does not mention {missing} — the doc and "
        "isar.schema.generators.FAMILY_GENERATORS have drifted")
