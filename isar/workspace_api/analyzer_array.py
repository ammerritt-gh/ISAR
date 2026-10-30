"""The analyzer array's Qt-free read (C-array-view): one scan set as the grid of its analyzers.

Read-only, and it degrades rather than raising on a thin workspace. A cell's identity is its spectrum key
(``experiment:scan:analyzer``); the layout only labels and places cells, so nothing here is stored against data.
The layout resolver lives in ``isar.schema.analyzer_layout`` (stdlib, shared with the inter-spectra grid).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from isar.schema.analyzer_layout import AnalyzerLayout, LayoutChoice, layout_for
from .scan_keys import scan_prefix
from .scan_list import Fact, ScanListSnapshot, _fact, _missing

__all__ = ["AnalyzerLayout", "LayoutChoice", "layout_for", "ArrayCell", "StepPair", "StepSummary",
           "ArrayHeader", "ArrayView", "array_view"]

_NO_FILE = "no file for this analyzer in this scan"
_NO_SAMPLE = "no sample name in the card"


@dataclass(frozen=True)
class ArrayCell:
    internal: str                 # "A4"
    beamline: str                 # "a06"
    row: int                      # 0 = row A
    pos: int                      # 0-based; 0 = in-plane position 1 (lowest 2theta)
    screen_row: int               # 0 = top of the grid
    screen_col: int               # 0 = left of the grid (highest 2theta)
    key: str | None               # the spectrum key; None = "no file" (not selectable)
    hkl: Fact                     # SpectrumRow.q, rlu
    q_abs: Fact                   # SpectrumRow.q_abs, nm^-1


@dataclass(frozen=True)
class StepPair:
    a: str                        # internal name of the lower neighbour
    b: str                        # internal name of the next one
    delta: tuple[float, float, float]     # hkl(b) - hkl(a), rlu


@dataclass(frozen=True)
class StepSummary:
    mean: tuple[float, float, float] | None   # None when no pair is used
    used: int                                 # pairs where both cells have an hkl
    possible: int                             # neighbour pairs the layout has
    pairs: tuple[StepPair, ...]               # the used pairs, layout order


@dataclass(frozen=True)
class ArrayHeader:
    instrument: str
    experiment_id: str | None
    scan_label: str
    scan_numbers: tuple[int, ...]
    sample: Fact
    temperature: Fact
    hkl_nominal: Fact
    n_spectra: int


@dataclass(frozen=True)
class ArrayView:
    set_key: str
    header: ArrayHeader
    layout: AnalyzerLayout | None
    how: str
    cells: tuple[ArrayCell, ...]
    unplaced: tuple[str, ...]     # spectrum keys whose analyzer the layout does not list
    in_plane: StepSummary         # along a row, toward rising 2theta
    out_of_plane: StepSummary     # up a column, row r to r+1

    def cell_for(self, key: str) -> ArrayCell | None:
        return next((c for c in self.cells if c.key == key), None)


def _hkl3(fact: Fact) -> tuple[float, float, float] | None:
    v = fact.value
    if isinstance(v, tuple) and len(v) == 3 and all(isinstance(x, (int, float)) and math.isfinite(x) for x in v):
        return tuple(float(x) for x in v)
    return None


def _neighbour_pairs(layout: AnalyzerLayout):
    """(in-plane, out-of-plane) neighbour pairs as (lower, next) beamline names, in layout order."""
    in_plane = [(names[n], names[n + 1]) for names in layout.rows for n in range(len(names) - 1)]
    out_of_plane = [(layout.rows[r][c], layout.rows[r + 1][c])
                    for r in range(layout.n_rows - 1) for c in range(layout.n_cols)]
    return in_plane, out_of_plane


def _summary(layout: AnalyzerLayout, pairs, hkl: dict) -> StepSummary:
    used = []
    for a, b in pairs:
        ha, hb = hkl.get(a), hkl.get(b)
        if ha is not None and hb is not None:
            used.append(StepPair(layout.internal_name(a), layout.internal_name(b),
                                 tuple(y - x for x, y in zip(ha, hb))))
    mean = None if not used else tuple(sum(p.delta[i] for p in used) / len(used) for i in range(3))
    return StepSummary(mean, len(used), len(pairs), tuple(used))


def _set_facts(ws, set_key: str) -> dict:
    """The scan facts the files of this set carry, from the first scan of the set that has any."""
    for sv in getattr(ws, "scans", None) or ():
        prov = getattr(sv, "provenance", None)
        facts = prov.get("scan_facts") if isinstance(prov, dict) else None
        if facts and scan_prefix(getattr(sv, "scan_id", "")) == set_key:
            return facts
    return {}


def _sample(policy) -> Fact:
    sample = getattr(policy, "sample", None)
    name = sample.get("name") if isinstance(sample, dict) else None
    return Fact(name, None, "card", "card") if isinstance(name, str) and name else _missing(_NO_SAMPLE)


def array_view(snapshot: ScanListSnapshot, set_key: str, ws) -> ArrayView | None:
    """One scan set of ``snapshot`` as the grid of its analyzers; None when the snapshot has no such set.
    ``ws`` is the read surface (``.scans``, ``.card.policy``); a missing part degrades to what it lacks."""
    row = snapshot.scan_set(set_key)
    if row is None:
        return None
    policy = getattr(getattr(ws, "card", None), "policy", None)
    choice = layout_for(policy)
    layout = choice.layout
    header = ArrayHeader(
        instrument=getattr(policy, "instrument", None) or "",
        experiment_id=row.experiment_id, scan_label=row.label, scan_numbers=row.scan_numbers,
        sample=_sample(policy), temperature=row.temperature,
        hkl_nominal=_fact(_set_facts(ws, set_key).get("hkl_nominal"), "rlu"),
        n_spectra=len(row.spectra))
    if layout is None:
        none = StepSummary(None, 0, 0, ())
        return ArrayView(set_key, header, None, choice.how, (), tuple(sp.key for sp in row.spectra), none, none)

    by_det = {sp.analyzer: sp for sp in row.spectra}
    cells = []
    for r, names in enumerate(layout.rows):
        for p, name in enumerate(names):
            sp = by_det.get(name)
            cells.append(ArrayCell(
                internal=layout.internal_name(name), beamline=name, row=r, pos=p,
                screen_row=layout.n_rows - 1 - r, screen_col=layout.n_cols - 1 - p,
                key=sp.key if sp is not None else None,
                hkl=sp.q if sp is not None else _missing(_NO_FILE),
                q_abs=sp.q_abs if sp is not None else _missing(_NO_FILE)))
    hkl = {c.beamline: _hkl3(c.hkl) for c in cells if c.key is not None}
    in_pairs, out_pairs = _neighbour_pairs(layout)
    return ArrayView(
        set_key=set_key, header=header, layout=layout, how=choice.how, cells=tuple(cells),
        unplaced=tuple(sp.key for sp in row.spectra if layout.position(sp.analyzer) is None),
        in_plane=_summary(layout, in_pairs, hkl), out_of_plane=_summary(layout, out_pairs, hkl))
