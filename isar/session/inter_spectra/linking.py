"""Mode linking, session side (C-link-run): the fits in use of a set of scans, read as the linking core reads them,
and the link queue item that runs it. Reads only: the result leaves through ``emit`` for the one writer to save.

The core (``isar.analyze.coupled.linking``) is pure. This module reads the store: which fit each spectrum uses
(``authority.resolve``, the rule every reader uses), that fit's peaks, and the stored fast pass that gives the
energy window, the resolution and the recipe. Numerics and the workspace load inside the functions, as ``nodes.py``
does, so the module imports light.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from isar.analyze.coupled import linking as core
from isar.session.inter_spectra import authority, records as inter_records

if TYPE_CHECKING:
    from isar.analyze.coupled.types import Held

log = logging.getLogger(__name__)

LINK_LOCK = "link:all"                      # the pseudo-file every link item occupies: one link item at a time
FAST = "fast"                               # the fit id of a spectrum's solo fast pass
NOT_FITTED = "not fitted yet"
STALE_LOCK = "its locked fit is out of date"
UNREADABLE = "its stored fit cannot be read"
NO_RESOLUTION = "no resolution recorded in its stored fit"
OLD_RECIPE = "fitted with an older fitting recipe; run its fast pass again"


class _Left(Exception):
    """A spectrum linking leaves out: its plain reason, and the token of the fit refused (None: no fit)."""

    def __init__(self, reason: str, token: str | None = None):
        super().__init__(reason)
        self.reason, self.token = reason, token


@dataclass(frozen=True)
class LinkItem:                             # what the item process receives (the service's "link" object)
    item_id: str
    unit_key: str                           # "link:<link_id>:link"
    pass_kind: str                          # "link"
    cores: int
    link: dict                              # {"link_id", "scans" (scan keys), "series" (bool), "reason"}


def intensity_scale(unit: str, *, efficiency: float | None = None) -> float:
    """1 / the analyzer's recorded efficiency, so intensities compare as the phonon's intrinsic amplitude (design
    2.2, A6); 1.0 when the file gives none. Never fitted: no per-spectrum free factor."""
    if isinstance(efficiency, (int, float)) and math.isfinite(efficiency) and efficiency > 0:
        return 1.0 / efficiency
    return 1.0


def _fast_of(session, unit: str) -> dict | None:
    """The stored fast-pass record of a spectrum, None when it has none."""
    entry = session.get(unit)
    if entry is None:
        return None
    return ((entry.detectors or {}).get(unit.rsplit(":", 1)[1]) or {}).get("fast_pass")


def _facts(fast: dict) -> tuple[tuple[float, float], float] | None:
    """(energy window, resolution FWHM) of a stored fast record; None when it records neither."""
    prov = fast.get("prior_provenance") or {}
    fwhm = (fast.get("flag_statistics") or {}).get("fwhm")
    if fwhm is None:
        fwhm = (prov.get("energy_penalty") or {}).get("fwhm_meV")
    try:
        lo, hi = (float(x) for x in prov["energy_support_meV"])
        fwhm = float(fwhm)
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(lo) and math.isfinite(hi) and math.isfinite(fwhm) and fwhm > 0):
        return None
    return (lo, hi), fwhm


def _modes_of(rec: dict) -> list[dict]:
    """A fit record's peaks in ascending omega0, the order ``held_from_fit`` and the core index modes by."""
    return sorted(rec.get("peaks") or (), key=lambda p: float(p["omega0"]))


def _sound(peaks: Sequence[dict]) -> bool:
    """Every mode has a finite omega0 and Gamma above zero: the core's energy summary needs both (it raises otherwise)."""
    for p in peaks:
        try:
            o, g = float(p["omega0"]), float(p["Gamma"])
        except (KeyError, TypeError, ValueError):
            return False
        if not (math.isfinite(o) and o > 0 and math.isfinite(g) and g > 0):
            return False
    return True


def _stamp(fit: str, inputs: str | None, rec: dict) -> str:
    return core.fit_stamp(fit, inputs, [(float(p["omega0"]), float(p["Gamma"]), p.get("area_stokes"))
                                        for p in _modes_of(rec)])


def _fitted_inputs(session, unit: str) -> str | None:
    """The inputs hash a spectrum's stored fits were made under, as ``currentness.judge`` reads it."""
    entry = session.get(unit)
    return ((entry.provenance or {}).get("fit_inputs") or {}).get("hash") if entry is not None else None


def _fit_record(root, session, unit: str, fit: str, lines: Mapping, records: Mapping | None) -> dict:
    """The record of a fit: the stored fast pass, a refit's stored overlay, or one the caller holds (not yet stored)."""
    if records is not None and fit in records:
        rec = records[fit]
    elif fit == FAST:
        rec = _fast_of(session, unit)
    else:
        line = lines.get(fit)
        payload = None if line is None else inter_records.read_payload(root, line)
        rec = ((payload or {}).get("overlay") or {}).get("fast_pass")
    if not rec:
        raise _Left(UNREADABLE)
    return rec


def _efficiency(session, unit: str) -> float | None:
    entry = session.get(unit)
    fact = ((entry.provenance or {}).get("analyzer_facts") or {}).get("efficiency") if entry is not None else None
    return fact.get("value") if isinstance(fact, dict) else None


def _in_use(root, card, session, units: Sequence[str], lines: Mapping) -> tuple[dict, dict]:
    """(plan, left): each unit's fit in use as (fit, inputs hash, T), and the units left out before a fit is read."""
    from isar.intake import gpi, inputs, scans as intake_scans, store
    from isar.intake.inter_spectra import read_user
    from isar.session.inter_spectra.nodes import read_nodes

    nodes = read_nodes(root).nodes
    user = read_user(root)
    by_unit: dict[str, list] = {}
    for r in lines.values():
        by_unit.setdefault(r.unit, []).append(r)
    rows = intake_scans.read_scans(root)
    read = store.read_all(root, store.ENTRIES)
    index = inputs.index_entries(read.entries, lines=read.lines)
    needed = sorted({*units, *(c.unit for u in units for r in by_unit.get(u, ()) for c in r.conditioned_on)})
    now = inter_records.inputs_now_from(rows, index, card.state.tabulated_calibration, needed,
                                        gpi=gpi.entries_in_force(root), data_dir=Path(root) / "data")
    plan: dict[str, tuple[str, str | None, float | None]] = {}
    left: dict[str, tuple[str, str | None]] = {}
    for unit in units:
        node = nodes.get(unit)
        if node is None:
            left[unit] = ("unknown to the import record", None)
            continue
        if not node.eligible:
            left[unit] = (node.reason or NOT_FITTED, None)
            continue
        res = authority.resolve(unit, refits=by_unit.get(unit, ()), pick=user.picks.get(unit),
                                lock=user.locks.get(unit), has_fast=authority.has_fast_solo(session.get(unit), unit),
                                inputs_now=now)
        if res.fit is None:
            left[unit] = (NOT_FITTED, None)
        elif not res.conditions:
            line = lines.get(res.chosen)
            try:
                rec = _fit_record(root, session, unit, res.chosen, lines, None)
                token = _stamp(res.chosen, line.inputs if line.inputs is not None else now.get(unit), rec)
            except _Left:
                token = None
            left[unit] = (STALE_LOCK, token)
        else:
            inp = now.get(unit)
            if res.fit != FAST and lines[res.fit].inputs is not None:
                inp = lines[res.fit].inputs
            if res.fit == FAST and inp is not None and _fitted_inputs(session, unit) != inp:
                from isar.session.inter_spectra.runner import NOT_CURRENT   # the runner's refusal, not a copy of it
                rec = _fast_of(session, unit)
                left[unit] = (NOT_CURRENT, _stamp(FAST, _fitted_inputs(session, unit), rec) if rec else None)
                continue
            plan[unit] = (res.fit, inp, node.T)
    return plan, left


def _link_input(root, session, unit: str, fit: str, inp: str | None, T: float | None, held, lines: Mapping,
                records: Mapping | None, source, anchored: bool) -> core.LinkInput:
    """The core's view of one spectrum's fit; raises ``_Left`` with the reason when the spectrum cannot be linked."""
    from isar.analyze import prior
    from isar.analyze.coupled.refit import held_from_fit

    if source is not None:                  # a batch's own freeze (amendment A3): its record and FWHM
        rec, fwhm = source(unit, held)
    else:
        rec = _fit_record(root, session, unit, fit, lines, records)
        if not _sound(_modes_of(rec)):
            raise _Left(UNREADABLE)
        token = _stamp(fit, inp, rec)
        fast = _fast_of(session, unit)
        if fast is None:
            raise _Left(UNREADABLE, token)
        if (fast.get("prior_provenance") or {}).get("version") != prior.PRIOR_RECIPE_VERSION:
            raise _Left(OLD_RECIPE, token)
        facts = _facts(fast)
        if facts is None:
            raise _Left(NO_RESOLUTION, token)
        fwhm = facts[1]
        if held is None:
            try:
                held = held_from_fit(rec, unit=unit, fit=fit, inputs=inp, context=None, T=T, window=facts[0])
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("%s: its stored fit cannot be held: %s", unit, exc)
                raise _Left(UNREADABLE, token) from exc
    peaks = _modes_of(rec)
    if len(peaks) != len(held.modes):
        raise _Left(UNREADABLE, _stamp(held.fit, held.inputs, rec))
    return core.LinkInput(
        held=held, fwhm=fwhm,
        areas=tuple(p.get("area_stokes") for p in peaks), sigma_areas=tuple(p.get("sigma_area") for p in peaks),
        sigma_gammas=tuple(p.get("sigma_Gamma") for p in peaks),
        at_cap=tuple(bool(p.get("at_width_cap", False)) for p in peaks),
        stamp=_stamp(held.fit, held.inputs, rec), scale=intensity_scale(unit, efficiency=_efficiency(session, unit)),
        anchored=anchored)


def inputs_for(root, units: Sequence[str], *, held: Mapping[str, Held] | None = None,
               records: Mapping[str, dict] | None = None, anchors: Collection[str] = (),
               source: Callable[[str, Held], tuple[dict, float]] | None = None
               ) -> tuple[dict[str, core.LinkInput], dict[str, tuple[str, str | None]]]:
    """Each spectrum's ``LinkInput`` and the spectra left out, with the plain reason and the fit's token.

    ``held`` None: the fit in use of each unit, read from the store. ``held`` given: a batch's frozen Held map, whose
    fits are used as they are. A fit not yet stored is read from ``records[fit]``; ``source(unit, held)`` gives a
    batch's own record and resolution FWHM instead of the store (with ``held``). ``anchors``: units whose modes are
    fixed ids (``LinkInput.anchored``).
    """
    from isar.session.workspace import Workspace

    root = Path(root)
    card, session = Workspace.open(root).load_read_only()
    lines = {r.fit: r for r in inter_records.read_log(root).refits}
    if held is None:
        plan, left = _in_use(root, card, session, units, lines)
    else:
        plan = {u: (h.fit, h.inputs, h.T) for u, h in held.items() if u in units}
        left = {}
    inputs: dict[str, core.LinkInput] = {}
    excluded: dict[str, tuple[str, str | None]] = {}
    for unit in sorted(units):
        if unit in left:
            excluded[unit] = left[unit]
            continue
        if unit not in plan:
            excluded[unit] = (NOT_FITTED, None)
            continue
        fit, inp, T = plan[unit]
        h = None if held is None else held[unit]
        try:
            inputs[unit] = _link_input(root, session, unit, fit, inp, T, h, lines, records, source,
                                       unit in anchors)
        except _Left as exc:
            excluded[unit] = (exc.reason, exc.token)
    return inputs, excluded


def previous_for(root, scans: Collection[str]) -> tuple[core.LinkResult | None, int]:
    """The saved link result to continue from, and the next branch-name number: one past every stored record's
    (``B1`` when nothing is stored). Prefers the newest record whose scope covers every scan, so a series link keeps
    branches seen only at other temperatures; else the newest record overlapping any of them."""
    lines = inter_records.read_links(root)
    nxt = max((line.next_name for line in lines), default=1)
    want = set(scans)
    mine = [line for line in lines if want <= set(line.scope)] or [line for line in lines if set(line.scope) & want]
    return (inter_records.read_link_payload(root, mine[-1]) if mine else None), nxt


def widen_to_series(root, scans: Sequence[str]) -> tuple[str, ...]:
    """Every scan of the temperature series of each of ``scans`` (its own scan when it has none), natural order."""
    from isar.session.inter_spectra.nodes import _natural, read_nodes

    ns = read_nodes(root)
    series_of = {n.scan: n.series for n in ns.nodes.values()}
    out: set[str] = set()
    for scan in scans:
        series = series_of.get(scan)
        out |= set(ns.series.get(series, (scan,)) if series is not None else (scan,))
    return tuple(sorted(out, key=_natural))


def link_scans(root, scans: Sequence[str]) -> core.LinkResult:
    """Link the fits in use of every analyzer of ``scans``, over q and over temperature. Reads only."""
    from isar.session.inter_spectra import graph, layout, nodes, q_provider
    from isar.session.workspace import Workspace
    from isar.intake.inter_spectra import read_inter_settings

    root = Path(root)
    card, _ = Workspace.open(root).load_read_only()
    settings = read_inter_settings(root)
    ns = nodes.read_nodes(root)
    units = sorted(k for k, n in ns.nodes.items() if n.scan in scans)
    qc = q_provider.q_coordinates(root, units)
    g = graph.build_graph(ns.nodes, ns, scope=units, kinds=frozenset({"q", "T"}), grid=layout.grid_for(card.policy.plugin),
                          coords=qc.points, collinearity_tol=settings.collinearity_tol, q_reasons=qc.reasons)
    inputs, excluded = inputs_for(root, units)
    previous, next_name = previous_for(root, scans)
    return core.link(inputs, g.links, settings, previous, next_name=next_name, excluded=excluded)


def _left_words(excluded: Mapping[str, tuple[str, str | None]]) -> str:
    if not excluded:
        return ""
    reasons = "; ".join(sorted({why for why, _ in excluded.values()}))
    return f"; {len(excluded)} spectra left out ({reasons})"


def run_link(workspace_root, item: LinkItem, *, progress, emit) -> None:
    """The link item (the service's ``--link-target``): link the item's scans and emit its one result. Never raises."""
    from isar.session.file_fit import FileResult

    def result(status: str, reason: str, payload: dict | None = None) -> None:
        emit(FileResult(item.item_id, item.unit_key, "link", status, reason, payload))

    try:
        scans = tuple(item.link["scans"])
        if item.link.get("series"):
            scans = widen_to_series(workspace_root, scans)
        progress({"pass": "link", "phase": "link"})
        r = link_scans(workspace_root, scans)
    except Exception as exc:  # noqa: BLE001 — one item's failure is its result, never the service's
        log.exception("link: %s failed", item.unit_key)
        return result("failed", f"link failed ({type(exc).__name__}: {exc})")
    if not r.made_from:
        return result("held", f"no spectrum of these scans has a usable fit{_left_words(r.excluded)}")
    summary = (f"{len(scans)} scans, {len(r.made_from)} spectra: {len(r.branches)} branches, {len(r.ties)} near "
               f"tie{'' if len(r.ties) == 1 else 's'}, {len(r.contradictions)} contradictions"
               f"{_left_words(r.excluded)}")
    result("done", summary, {"schema": inter_records.SCHEMA_LINK, "unit": item.unit_key, "pass": "link",
                             "result": core.to_json(r), "scope": list(scans), "batch": None, "stage": None})
