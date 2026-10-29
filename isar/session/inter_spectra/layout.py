"""The analyzer layout that gives q-neighbours (C-graph). Stdlib only; S1U4 fills the methods.

BL43LXU's 4 x 7 grid: detector ids left to right, top to bottom. Grid adjacency is same row +-1 column or
same column +-1 row, no diagonals (assumption A1).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Grid:
    rows: tuple[tuple[str, ...], ...]               # detector ids left to right, top to bottom

    def position(self, det: str) -> tuple[int, int] | None:          # (row, col)
        raise NotImplementedError("stub: filled by S1U4")

    def adjacent(self, det: str) -> tuple[str, ...]:                 # same row +-1 col; same col +-1 row
        raise NotImplementedError("stub: filled by S1U4")

    def lines(self) -> tuple[tuple[str, tuple[str, ...]], ...]:      # ("row 1", dets) and ("col 3", dets)
        raise NotImplementedError("stub: filled by S1U4")


BL43_GRID = Grid(rows=(
    ("a03", "a04", "a05", "a06", "a07", "a08", "a09"),
    ("a14", "a15", "a16", "a17", "a18", "a19", "a20"),
    ("a25", "a26", "a27", "a28", "a29", "a30", "a31"),
    ("a35", "a36", "a37", "a38", "a39", "a40", "a41"),
))


def grid_for(plugin: str) -> Grid | None:        # "sp8_bl43" -> BL43_GRID; any other plugin -> None
    raise NotImplementedError("stub: filled by S1U4")
