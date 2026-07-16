"""isar_gui.group_targets — destination validation for "use this group" drops (GUI Pass 2, Part 2A).

A manual group is a dumb set of scan-unit ids. When it is dropped on a plot dock, the DESTINATION
decides whether it can render it, and refuses LOUDLY with a specific reason otherwise (plan
R-accept). This module is that decision, kept **Qt-free** so it is unit-tested directly and shared
by every consuming dock via its viewmodel.

The engine never learns about "groups" — only unit ids cross into :mod:`isar.workspace_api`. A drop
compiles to a restriction over the same "on the plane" population the views already derive
(:func:`isar_gui.viewmodels.projection.candidates` + :func:`~.projection.restrict`), so validation
and rendering can never disagree about which scans a group actually covers.

Targets:
  * ``"waterfall"`` — the waterfall/comparison dock. Accepts any fitted group (both modes handle
    multiple temperatures); the dock reports a hidden-q count on the status bar, never refuses on T.
  * ``"dispersion"`` / ``"heatmap"`` — one displayed temperature. A group spanning >1 T is refused
    with the temperatures named, so the plane never silently omits the other Ts.
"""
from __future__ import annotations

#: Targets that show a single temperature at a time and so refuse a multi-T group.
_SINGLE_T_TARGETS = ("dispersion", "heatmap")


def covered(results, scan_ids) -> list:
    """The fitted candidates (plane population) a group of ``scan_ids`` covers — the exact set the
    restricted view will render.

    ``projection`` is imported lazily: the view VMs import this module during package init, so a
    top-level ``from isar_gui.viewmodels import projection`` here would risk a partial-init cycle."""
    from isar_gui.viewmodels import projection
    return projection.restrict(projection.candidates(results), set(scan_ids))


def temperatures(results, scan_ids) -> list:
    """Sorted distinct (6-dp) temperatures among the covered fits."""
    return sorted({round(fv.T, 6) for fv in covered(results, scan_ids) if fv.T is not None})


def accepts(target: str, results, scan_ids) -> tuple[bool, str]:
    """``(ok, reason)`` for dropping ``scan_ids`` on ``target``. ``reason`` is a specific,
    operator-facing refusal when ``ok`` is False, else ``""`` (plan R-accept)."""
    ids = [str(k) for k in scan_ids]
    n = len(ids)
    fits = covered(results, ids)
    if not fits:
        return False, f"none of these {n} scan(s) are fitted yet — nothing to plot."
    if target in _SINGLE_T_TARGETS:
        temps = sorted({round(fv.T, 6) for fv in fits if fv.T is not None})
        if len(temps) > 1:
            tlist = ", ".join(f"{t:g}" for t in temps)
            return False, (f"these {len(fits)} scan(s) span T = {tlist} K; the plane shows one "
                           "temperature — drop on Waterfall/Comparison, or narrow to one T.")
    return True, ""
