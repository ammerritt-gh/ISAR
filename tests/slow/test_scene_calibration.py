"""Placeholder harness for the SCENE calibration battery (isar/synth/ensemble.py).

The battery this will become answers calibration questions — "of the worlds with no
acoustic branch, how often did the pipeline claim one?" — over many seeds. That needs
fits, which SCENE-0 does not have. What is pinned here is the part the battery will stand
on regardless: the batch is deterministic, correctly labeled, and round-trips through
disk, so a future calibration number can never be a bookkeeping artifact.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from isar.synth import ensemble

WORLD_NAMES = ("canonical", "no_acoustic")
SEEDS = (11, 12, 13)


@pytest.mark.slow
def test_ensemble_batch_shape_and_labels(tmp_path):
    batch = ensemble.run_ensemble(WORLD_NAMES, SEEDS, out_dir=tmp_path)
    assert len(batch) == len(WORLD_NAMES) * len(SEEDS)
    # Worlds-major, seeds in the order given — the battery's row order is part of its contract.
    assert [(r.world, r.seed) for r in batch] == [(w, s) for w in WORLD_NAMES for s in SEEDS]

    for item in batch:
        assert item.truth.name == item.world and item.truth.seed == item.seed
        assert item.data.counts.shape == item.data.lam.shape
        assert item.data.counts.shape[0] == len(item.data.H)
        assert item.data.counts.shape[1] == len(item.data.x)

    rows = ensemble.index(batch)
    assert {tuple(r["identities"]) for r in rows if r["world"] == "no_acoustic"} == {
        ("bragg", "gapped", "optic")
    }
    assert all(r["n_scans"] == 21 and r["n_channels"] == 81 for r in rows)


@pytest.mark.slow
def test_ensemble_is_deterministic_and_seed_separated():
    first = ensemble.run_ensemble(WORLD_NAMES, SEEDS)
    second = ensemble.run_ensemble(WORLD_NAMES, SEEDS)
    for a, b in zip(first, second):
        assert np.array_equal(a.data.counts, b.data.counts)

    canonical = [r for r in first if r.world == "canonical"]
    for a, b in zip(canonical, canonical[1:]):
        assert np.array_equal(a.data.lam, b.data.lam)          # same world
        assert not np.array_equal(a.data.counts, b.data.counts)  # different seed

    # A subset of an ensemble is the corresponding subset of the whole.
    subset = ensemble.run_ensemble(("no_acoustic",), (12,))
    match = next(r for r in first if (r.world, r.seed) == ("no_acoustic", 12))
    assert np.array_equal(subset[0].data.counts, match.data.counts)


@pytest.mark.slow
def test_ensemble_round_trips_through_disk(tmp_path):
    batch = ensemble.run_ensemble(WORLD_NAMES, SEEDS, out_dir=tmp_path)
    index_path = ensemble.write_index(batch, tmp_path / "index.json")
    assert index_path.is_file()
    with index_path.open(encoding="utf-8") as fh:
        assert json.load(fh) == ensemble.index(batch)

    for item in batch:
        restored = ensemble.read_realization(
            ensemble.realization_dir(tmp_path, item.world, item.seed))
        assert restored.world == item.world and restored.seed == item.seed
        assert restored.truth == item.truth
        assert np.array_equal(restored.data.counts, item.data.counts)
        assert np.array_equal(restored.data.lam, item.data.lam)
        assert np.array_equal(restored.data.H, item.data.H)
        assert np.array_equal(restored.data.x, item.data.x)
