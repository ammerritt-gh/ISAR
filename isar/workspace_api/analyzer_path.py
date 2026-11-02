"""The analyzer path, read (C-path): the analyzers picked in order, placed along the path they make.

Each stop is one picked analyzer of one scan. Its position is the cumulative distance between the graph's own q
points (cartesian inverse angstrom when every stop has a lattice epoch, else hkl). Its modes are the fit the caller
chooses (the fit in use), so a refit draws its own modes. Its phonon names come from the saved links only while they
name that fit.
Read-only and Qt-free; a thin workspace gives ``ok=False`` with a plain reason, never an exception.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from isar.session.inter_spectra.q_provider import q_coordinates

from .analyzer_array import ArrayView, array_view
from .inter_spectra import LinkView, UnitLinks
from .scan_list import Fact, ScanListSnapshot, SpectrumRow
from .spectra import SpectraCache, spectrum_bundles

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PathMode:
    mode: int                              # 0-based index in the fit in use (ascending omega0)
    energy: float | None                   # omega0 (meV) with its interval: where the mode is drawn
    peak_E: float | None                   # E* (meV): the energy the links matched on; None unless a current link names this fit
    lo: float | None
    hi: float | None
    width: float | None                    # Gamma (meV)
    name: str | None


@dataclass(frozen=True)
class PathStop:
    key: str
    beamline: str                          # "a06"
    internal: str                          # "A4"; "" when the layout does not place the analyzer
    x: float | None                        # cumulative distance along the path, in the view's metric
    modes: tuple[PathMode, ...]
    note: str | None                       # why a stop has no modes, or that it draws a fit other than the one picked


@dataclass(frozen=True)
class PathView:
    ok: bool
    reason: str | None
    set_key: str | None
    scan_label: str
    temperature: Fact | None
    metric: Literal["lattice", "hkl"] | None
    unit: str                              # "Å⁻¹" for lattice, "rlu" for hkl, "" when not ok
    stops: tuple[PathStop, ...]
    stale: bool                            # a saved link covering a stop is out of date
    stale_text: str | None
    links_at: str | None                   # the time of the saved link the names come from
    skipped: tuple[str, ...]               # picked keys left out: not spectra, or of another scan


def _no(reason: str, *, set_key=None, label="", temperature=None, skipped=()) -> PathView:
    return PathView(False, reason, set_key, label, temperature, None, "", (), False, None, None, tuple(skipped))


def _place(view: ArrayView, row: SpectrumRow) -> tuple[str, str]:
    cell = view.cell_for(row.key)
    return (cell.beamline, cell.internal) if cell is not None else (row.analyzer or row.key, "")


def _mode(i: int, mode, ul: UnitLinks | None) -> PathMode:
    named = ul is not None
    at_E = named and not ul.stale           # a stale record's E* is not drawn: its mode stays at omega0
    return PathMode(
        mode=i, energy=mode.energy.value, peak_E=ul.peak_E[i] if at_E else None,
        lo=mode.energy.lo, hi=mode.energy.hi, width=mode.linewidth.value,
        name=ul.names[i] if named else None)


def analyzer_path(snapshot: ScanListSnapshot, ws, keys: Sequence[str], *, links: LinkView | None = None,
                  cache: SpectraCache | None = None, fit_choice: Mapping[str, str] | None = None) -> PathView:
    """The path the picked ``keys`` make, in the order given (duplicates dropped). The scan is the first spectrum's;
    keys of other scans, and keys that are not spectra, go to ``skipped``. ``fit_choice`` is the fit each key draws
    (the view's rule, ``InterState.fit_choice``); a key absent from it draws the Fast solo."""
    picked = tuple(dict.fromkeys(keys))
    rows = [r for r in (snapshot.spectrum(k) for k in picked) if isinstance(r, SpectrumRow)]
    if not rows:
        return _no("none of the picked analyzers is a spectrum of this experiment", skipped=picked)
    set_key = rows[0].set_key
    members = [r for r in rows if r.set_key == set_key]
    skipped = tuple(k for k in picked if k not in {r.key for r in members})
    scan_set = snapshot.scan_set(set_key)
    label, temperature = (scan_set.label, scan_set.temperature) if scan_set is not None else ("", None)
    where = dict(set_key=set_key, label=label, temperature=temperature, skipped=skipped)
    if len(members) < 2:
        return _no(f"pick at least two analyzers of one scan ({len(members)} picked)", **where)
    view = array_view(snapshot, set_key, ws)
    if view is None or view.layout is None:
        return _no("this instrument has no analyzer layout, so its analyzers have no path", **where)
    try:
        q = q_coordinates(ws.root, [r.key for r in members])
    except Exception as exc:                 # a thin workspace has no card or session to read the q from
        log.warning("analyzer path: the q of %s cannot be read (%s: %s)", set_key, type(exc).__name__, exc)
        return _no("the analyzers' q cannot be read from this workspace", **where)
    for r in members:
        if r.key not in q.points:
            return _no(f"{_place(view, r)[0]} has no point in q: {q.reasons.get(r.key, 'no accepted hkl')}", **where)

    points = [q.points[r.key] for r in members]
    if all(p.cart is not None for p in points):
        metric, unit, coords = "lattice", "Å⁻¹", [p.cart for p in points]
    else:
        metric, unit, coords = "hkl", "rlu", [p.hkl for p in points]
    xs = [0.0]
    for a, b in zip(coords, coords[1:]):
        xs.append(xs[-1] + math.dist(a, b))

    bundles = spectrum_bundles(ws, members, cache=cache, fit_choice=fit_choice)
    stops, stale_text, links_at, stale = [], None, None, False
    for r, x, bundle in zip(members, xs, bundles):
        ul = None if links is None else links.by_unit.get(r.key)
        if ul is not None:
            stale = stale or ul.stale
            stale_text = stale_text or ul.stale_text
            links_at = links_at or (ul.at or None)
        named = ul is not None and ul.current and ul.names and ul.fit_id == bundle.fit_id
        fast = bundle.summaries["fast"]
        modes = tuple(_mode(i, m, ul if named else None) for i, m in enumerate(fast.modes))
        wanted = (fit_choice or {}).get(r.key, "fast")
        fallback = (f"fit {wanted} cannot be drawn; showing the Fast solo fit, without phonon names"
                    if wanted != bundle.fit_id else None)
        why = None if modes else (bundle.not_drawn.text if bundle.not_drawn is not None else "no fit to draw")
        note = "; ".join(n for n in (fallback, why) if n) or None
        beamline, internal = _place(view, r)
        stops.append(PathStop(r.key, beamline, internal, x, modes, note))
    if not any(s.modes for s in stops):
        return _no("none of the picked analyzers has a fit in use to draw", **where)
    return PathView(True, None, set_key, label, temperature, metric, unit, tuple(stops), stale, stale_text,
                    links_at, skipped)
