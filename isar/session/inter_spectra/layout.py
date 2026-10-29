"""The analyzer layout that gives q-neighbours (C-graph). Stdlib only.

BL43LXU's 4 x 7 grid: detector ids left to right, top to bottom. Grid adjacency is same row +-1 column or
same column +-1 row, no diagonals (assumption A1).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Grid:
    rows: tuple[tuple[str, ...], ...]               # detector ids left to right, top to bottom

    def position(self, det: str) -> tuple[int, int] | None:          # (row, col)
        for r, dets in enumerate(self.rows):
            if det in dets:
                return r, dets.index(det)
        return None

    def adjacent(self, det: str) -> tuple[str, ...]:                 # same row +-1 col; same col +-1 row
        pos = self.position(det)
        if pos is None:
            return ()
        r, c = pos
        return tuple(self.rows[rr][cc] for rr, cc in ((r - 1, c), (r, c - 1), (r, c + 1), (r + 1, c))
                     if 0 <= rr < len(self.rows) and 0 <= cc < len(self.rows[rr]))

    def lines(self) -> tuple[tuple[str, tuple[str, ...]], ...]:      # ("row 1", dets) and ("col 3", dets)
        cols = tuple((f"col {c + 1}", tuple(row[c] for row in self.rows)) for c in range(len(self.rows[0])))
        return tuple((f"row {r + 1}", dets) for r, dets in enumerate(self.rows)) + cols


BL43_GRID = Grid(rows=(
    ("a03", "a04", "a05", "a06", "a07", "a08", "a09"),
    ("a14", "a15", "a16", "a17", "a18", "a19", "a20"),
    ("a25", "a26", "a27", "a28", "a29", "a30", "a31"),
    ("a35", "a36", "a37", "a38", "a39", "a40", "a41"),
))


def grid_for(plugin: str) -> Grid | None:        # "sp8_bl43" -> BL43_GRID; any other plugin -> None
    # shortcut: the layout is a code constant, not a card key; move it to the card when a second instrument appears
    return BL43_GRID if plugin == "sp8_bl43" else None
