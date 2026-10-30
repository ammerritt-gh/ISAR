"""Where each analyzer sits in an instrument's array, and the one resolver every consumer asks for it. Stdlib only.

The layout is format knowledge: an instrument card's `[[analyzer_layouts]]` tables, shape-checked by `Card.validate`.
Row A is the bottom row (the one in the scattering plane); within a row, position 1 is the lowest 2theta. An internal
name (A4) is a label derived from those positions and is never stored against data, so a layout change re-labels
cells and moves no fit, anchor or lock.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_ROWS = 26          # internal row letters A..Z


@dataclass(frozen=True)
class AnalyzerLayout:
    id: str
    rows: tuple[tuple[str, ...], ...]      # beamline names; rows[0] is row A, each row position 1 first

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return len(self.rows[0])

    def position(self, det: str) -> tuple[int, int] | None:     # (row index, position index), both 0-based
        for r, names in enumerate(self.rows):
            if det in names:
                return r, names.index(det)
        return None

    def internal_name(self, det: str) -> str | None:             # "A4" for a06
        pos = self.position(det)
        return None if pos is None else f"{chr(ord('A') + pos[0])}{pos[1] + 1}"


@dataclass(frozen=True)
class LayoutChoice:
    layout: AnalyzerLayout | None
    how: str          # "the only BL43LXU layout" | "no analyzer layout for IN8" | "layout unknown"


def layout_problems(entry) -> list[str]:
    """Shape problems of one `[[analyzer_layouts]]` table; none when it reads as a rectangular layout.
    Whether its names are detectors of the card is the card's check, not this one's."""
    if not isinstance(entry, dict):
        return ["is not a table"]
    problems = []
    if not isinstance(entry.get("id"), str) or not entry["id"]:
        problems.append("id must be a non-empty string")
    rows = entry.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        return problems + [f"rows must be a list of 1 to {MAX_ROWS} rows"]
    if not all(isinstance(r, list) and r and all(isinstance(n, str) and n for n in r) for r in rows):
        return problems + ["each row must be a non-empty list of analyzer names"]
    if len({len(r) for r in rows}) > 1:
        problems.append("every row must have the same number of analyzers")
    names = [n for r in rows for n in r]
    problems += [f"analyzer {n!r} is listed twice" for n in sorted({n for n in names if names.count(n) > 1})]
    return problems


def layout_for(policy, set_key: str | None = None) -> LayoutChoice:
    """The layout a scan set uses, and how it was chosen. ``set_key`` is unused until a per-scan assignment exists
    (deferred by the design). A card with more than one layout gives none, so the grid never guesses."""
    instrument = getattr(policy, "instrument", None) or "this instrument"
    raw = getattr(policy, "analyzer_layouts", None)
    entries = raw if isinstance(raw, list) else []
    if not entries:
        return LayoutChoice(None, f"no analyzer layout for {instrument}")
    if len(entries) > 1:
        return LayoutChoice(None, "layout unknown")
    entry = entries[0]
    problems = layout_problems(entry)
    if problems:
        log.warning("analyzer layout of %s is malformed and is not used: %s", instrument, "; ".join(problems))
        return LayoutChoice(None, "layout unknown")
    return LayoutChoice(AnalyzerLayout(entry["id"], tuple(tuple(r) for r in entry["rows"])),
                        f"the only {instrument} layout")
