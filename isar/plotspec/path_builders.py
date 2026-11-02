"""The analyzer path's plot spec (design 2.5): position along the picked path against energy.

Plain data in. The view model converts the facade's ``PathView`` to the dicts below, so this module imports no
workspace_api. A stop is ``{"x", "beamline", "note", "modes"}``; a mode is ``{"energy", "lo", "hi", "peak_E", "name"}``,
where ``name`` is the mode's phonon label (P3) when a current saved link names it, else None.
A named mode is drawn at its peak energy E* (the energy its link matched on) with no whisker; every other mode is
drawn at omega0 with its stored interval. Each phonon is one series in its own colour, joined by a line between
path-adjacent stops that both carry it; a lone phonon is one such series too.
"""
from __future__ import annotations

import numpy as np

from isar.plotspec import spec as ps

UNSAVED_LABEL = "modes (no saved links)"
UNSAVED_TIP = "Link modes has not covered this analyzer"
PHONON_TIP = "Phonons sit at their peak energy E*, the energy the links matched on; other modes at omega0."


def _row(x: float, mode: dict) -> tuple[float, float, float, float]:
    """(x, y, below, above): a named mode at its E* with no whisker; any other at omega0 with its interval."""
    if mode["peak_E"] is not None and mode["name"] is not None:
        return float(x), float(mode["peak_E"]), 0.0, 0.0
    y = float(mode["energy"])
    lo, hi = mode["lo"], mode["hi"]
    below = max(y - lo, 0.0) if lo is not None else 0.0
    above = max(hi - y, 0.0) if hi is not None else 0.0
    return float(x), y, below, above


def _series(rows, **kw) -> ps.Series:
    x, y, below, above = (np.array(col, dtype=float) for col in zip(*rows))
    return ps.Series(x=x, y=y, errors=ps.ErrorBars(bottom=below, top=above), **kw)


def analyzer_path_spec(*, stops, title, xlabel, ylabel, stale_note=None, reason=None) -> ps.PlotSpec:
    """The path plot. ``reason`` makes it an empty spec with one annotation saying why the path is unavailable."""
    spec = ps.PlotSpec(title=title, xlabel=xlabel, ylabel=ylabel)
    if reason is not None:
        spec.annotations.append(ps.Annotation(text=reason))
        return spec
    xs = [float(stop["x"]) for stop in stops]
    spec.x_ticks = [(x, stop["beamline"]) for x, stop in zip(xs, stops)]
    # one corner note for the stale words and the stops' notes: unanchored notes all stack in one corner
    notes = [stale_note] if stale_note else []
    phonons: dict[str, list[tuple[int, dict]]] = {}      # phonon label -> (stop index, mode), in path order
    unsaved = []
    for i, stop in enumerate(stops):
        if stop["note"]:
            notes.append(f"{stop['beamline']}: {stop['note']}")
        for mode in stop["modes"]:
            if mode["energy"] is None:
                continue
            if mode["name"] is None:
                unsaved.append(_row(xs[i], mode))
            else:
                phonons.setdefault(mode["name"], []).append((i, mode))
    if notes:
        spec.annotations.append(ps.Annotation(text=" · ".join(notes)))

    for k, name in enumerate(sorted(phonons, key=lambda p: int(p[1:]))):     # the colours of Scan(s): ascending number
        placed = phonons[name]
        colour = ps.TRACE_COLORS[k % len(ps.TRACE_COLORS)]
        spec.series.append(_series([_row(xs[i], m) for i, m in placed], label=name,
                                   symbol=ps.SYMBOLS[k % len(ps.SYMBOLS)], color=colour, tooltip=PHONON_TIP))
        first = {}
        for i, mode in placed:
            first.setdefault(i, _row(xs[i], mode))
        for i in range(len(stops) - 1):          # a line only between path-adjacent stops that both carry the phonon
            if i in first and i + 1 in first:
                spec.series.append(ps.Series(
                    x=np.array([xs[i], xs[i + 1]]), y=np.array([first[i][1], first[i + 1][1]]),
                    role=ps.Role.BRANCH, color=colour))
    if unsaved:
        spec.series.append(_series(unsaved, label=UNSAVED_LABEL, style="muted", tooltip=UNSAVED_TIP))
    return spec
