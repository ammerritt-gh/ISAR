"""The analyzer path's plot spec (C-link-plotvocab, design 2.5): position along the picked path against energy.

Plain data in. The view model converts the facade's ``PathView`` to the dicts below, so this module imports no
workspace_api. A stop is ``{"x", "beamline", "note", "modes"}``; a mode is ``{"energy", "lo", "hi", "peak_E", "name",
"unresolved", "markers"}``; a marker is ``{"group", "label", "words"}`` with group 2 (change) or 3 (unlinked).
Modes are drawn at the fit's omega0 with its interval; link markers sit at the mode's peak energy E*, never omega0.
"""
from __future__ import annotations

import numpy as np

from isar.plotspec import spec as ps

UNLINKED_LABEL = "unlinked modes"
UNRESOLVED_LABEL = "unresolved (two branches)"
_GROUP_KIND = {2: ps.MarkerKind.LINK_CHANGE, 3: ps.MarkerKind.LINK_UNLINKED}


def _row(x: float, mode: dict) -> tuple[float, float, float]:
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
    unlinked, unresolved = [], []
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
                unresolved.append(_row(xs[i], mode))
                spec.annotations.append(ps.Annotation(
                    text=" / ".join(mode["unresolved"]) + "?", xy=(xs[i], mode["energy"])))
            elif mode["name"] is None:
                unlinked.append(_row(xs[i], mode))
            else:
                branch_modes.setdefault(mode["name"], []).append((i, mode))
    if notes:
        spec.annotations.append(ps.Annotation(text=" · ".join(notes)))

    for k, (name, placed) in enumerate(branch_modes.items()):
        colour = ps.TRACE_COLORS[k % len(ps.TRACE_COLORS)]
        spec.series.append(_series([_row(xs[i], m) for i, m in placed], label=name,
                                   symbol=ps.SYMBOLS[k % len(ps.SYMBOLS)], color=colour))
        first = {}
        for i, mode in placed:
            first.setdefault(i, mode)
        for i in range(len(stops) - 1):          # a line only between path-adjacent stops that both carry the branch
            if i in first and i + 1 in first:
                spec.series.append(ps.Series(
                    x=np.array([xs[i], xs[i + 1]]), y=np.array([first[i]["energy"], first[i + 1]["energy"]]),
                    role=ps.Role.BRANCH, color=colour))
    if unlinked:
        spec.series.append(_series(unlinked, label=UNLINKED_LABEL, style="muted"))
    if unresolved:
        spec.series.append(_series(unresolved, label=UNRESOLVED_LABEL, style="muted"))
    return spec
