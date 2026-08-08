"""Labeled batches of SCENE worlds — the input side of the calibration battery.

A calibration statement ("this pipeline claims an acoustic branch in N% of worlds that
have none") needs many seeded realizations of many worlds, each carrying its own answer
key. That is all this module does: expand ``(world names) × (seeds)`` into realizations,
in a fixed order, optionally onto disk.

Deterministic by construction: a realization depends only on its world name and seed, so
re-running an ensemble reproduces it exactly, and a subset of an ensemble is identical to
the corresponding subset of the whole.

Serial on purpose — generating a world is ~0.1 s, so a battery of a few hundred is a
minute of one core. If that stops being true, parallelize with an explicit worker count
(no cpu-derived default: `isar/synth` is outside the repo's nested-pool guard).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from . import config, generate, worlds


@dataclass(frozen=True)
class Realization:
    """One (world, seed) draw and its answer key."""

    world: str
    seed: int
    data: generate.WorldData

    @property
    def truth(self) -> config.WorldTruth:
        return self.data.truth


def realize(world: str, seed: int) -> Realization:
    """Generate one labeled realization of a named world."""
    _cfg, truth = worlds.build(world, seed)
    return Realization(world=world, seed=int(seed), data=generate.generate_world(truth))


def run_ensemble(world_names: Sequence[str], seeds: Iterable[int], *,
                 out_dir: Path | str | None = None) -> list[Realization]:
    """Realize every (world, seed) pair, worlds-major, in the order given.

    With ``out_dir``, each realization is also written to
    ``<out_dir>/<world>/seed-<seed>/`` as ``truth.json`` + ``data.npz``.
    """
    seeds = [int(s) for s in seeds]
    batch = [realize(name, seed) for name in world_names for seed in seeds]
    if out_dir is not None:
        for item in batch:
            write_realization(item, out_dir)
    return batch


# ----------------------------------------------------------------------------
# On-disk form
# ----------------------------------------------------------------------------
def realization_dir(out_dir: Path | str, world: str, seed: int) -> Path:
    return Path(out_dir) / world / f"seed-{seed}"


def write_realization(item: Realization, out_dir: Path | str) -> Path:
    """Write one realization; returns its directory."""
    target = realization_dir(out_dir, item.world, item.seed)
    target.mkdir(parents=True, exist_ok=True)
    generate.write_world_truth(item.truth, target / "truth.json")
    np.savez(target / "data.npz", H=item.data.H, x=item.data.x,
             lam=item.data.lam, counts=item.data.counts)
    return target


def read_realization(path: Path | str) -> Realization:
    """Read back a realization written by `write_realization`."""
    path = Path(path)
    truth = generate.read_world_truth(path / "truth.json")
    with np.load(path / "data.npz") as npz:
        data = generate.WorldData(truth=truth, H=npz["H"], x=npz["x"],
                                  lam=npz["lam"], counts=npz["counts"])
    return Realization(world=truth.name, seed=truth.seed, data=data)


def index(batch: Sequence[Realization]) -> list[dict]:
    """A JSON-serializable label index for a batch (world, seed, identities, shape)."""
    return [
        {
            "world": item.world,
            "seed": item.seed,
            "T": item.truth.T,
            "identities": [c.identity for c in item.truth.components],
            "n_scans": int(item.data.counts.shape[0]),
            "n_channels": int(item.data.counts.shape[1]),
        }
        for item in batch
    ]


def write_index(batch: Sequence[Realization], path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index(batch), indent=2), encoding="utf-8")
    return path
