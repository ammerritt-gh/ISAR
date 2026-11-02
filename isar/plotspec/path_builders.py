"""The analyzer path's plot spec (C-link-plotvocab, design 2.5): position along the picked path against energy.

Plain data in. The view model converts the facade's ``PathView`` to the dicts below, so this module imports no
workspace_api. A stop is ``{"x", "beamline", "note", "covered", "modes"}``, ``covered`` true when a current saved link
covers the fit drawn there; a mode is ``{"energy", "lo", "hi", "peak_E", "name", "unresolved", "markers"}``; a marker
is ``{"group", "label", "words"}`` with group 2 (change) or 3 (unlinked).
A named or split mode (current link, not stale) is drawn at its peak energy E* with no whisker, the energy its link
matched on; every other mode is drawn at omega0 with its stored interval. Link markers sit at E*, never omega0.
"""
from __future__ import annotations

import numpy as np

from isar.plotspec import spec as ps

UNLINKED_LABEL = "unlinked modes"
UNSAVED_LABEL = "modes (no saved links)"
UNSAVED_TIP = "Link modes has not covered this analyzer"
UNRESOLVED_LABEL = "unresolved (two branches)"
BRANCH_TIP = "Named modes sit at their peak energy E*, the energy the links matched on; other modes at omega0."
_GROUP_KIND = {2: ps.MarkerKind.LINK_CHANGE, 3: ps.MarkerKind.LINK_UNLINKED}


def _row(x: float, mode: dict) -> tuple[float, float, float]:
    if mode["peak_E"] is not None and (mode["name"] is not None or mode["unresolved"]):
        return float(x), float(mode["peak_E"]), 0.0
    lo, hi = mode["lo"], mode["hi"]
    half = (hi - lo) / 2 if lo is not None and hi is not None else 0.0
    return float(x), float(mode["energy"]), max(half, 0.0)


def _series(rows, **kw) -> ps.Series:
    x, y, err = (np.array(col, dtype=float) for col in zip(*rows))
    return ps.Series(x=x, y=y, yerr=err, **kw)


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
    branch_modes: dict[str, list[tuple[int, dict]]] = {}     # branch name -> (stop index, mode), in order
    unlinked, unsaved, unresolved = [], [], []
    for i, stop in enumerate(stops):
        if stop["note"]:
            notes.append(f"{stop['beamline']}: {stop['note']}")
        for mode in stop["modes"]:
            if mode["peak_E"] is not None:
                for mk in mode["markers"]:
                    spec.markers.append(ps.Marker(
                        x=xs[i], y=mode["peak_E"], kind=_GROUP_KIND[mk["group"]], label=mk["label"],
                        tooltip=mk["words"]))
            if mode["energy"] is None:
                continue
            if mode["unresolved"]:
                row = _row(xs[i], mode)
                unresolved.append(row)
                spec.annotations.append(ps.Annotation(
                    text=" / ".join(mode["unresolved"]) + "?", xy=row[:2]))
            elif mode["name"] is None:
                (unlinked if stop["covered"] else unsaved).append(_row(xs[i], mode))
            else:
                branch_modes.setdefault(mode["name"], []).append((i, mode))
    if notes:
        spec.annotations.append(ps.Annotation(text=" · ".join(notes)))

    for k, (name, placed) in enumerate(branch_modes.items()):
        colour = ps.TRACE_COLORS[k % len(ps.TRACE_COLORS)]
        spec.series.append(_series([_row(xs[i], m) for i, m in placed], label=name,
                                   symbol=ps.SYMBOLS[k % len(ps.SYMBOLS)], color=colour, tooltip=BRANCH_TIP))
        first = {}
        for i, mode in placed:
            first.setdefault(i, _row(xs[i], mode))
        for i in range(len(stops) - 1):          # a line only between path-adjacent stops that both carry the branch
            if i in first and i + 1 in first:
                spec.series.append(ps.Series(
                    x=np.array([xs[i], xs[i + 1]]), y=np.array([first[i][1], first[i + 1][1]]),
                    role=ps.Role.BRANCH, color=colour))
    if unlinked:
        spec.series.append(_series(unlinked, label=UNLINKED_LABEL, style="muted"))
    if unsaved:
        spec.series.append(_series(unsaved, label=UNSAVED_LABEL, style="muted", tooltip=UNSAVED_TIP))
    if unresolved:
        spec.series.append(_series(unresolved, label=UNRESOLVED_LABEL, style="muted"))
    return spec
