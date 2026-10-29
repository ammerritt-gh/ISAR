"""Effective hkl and coordinates per spectrum for the graph (C-q-provider). Stdlib at import; the intake and geometry
modules load when a call runs.

A spectrum's point is its analyzer's accepted hkl (``inputs.analyzer_q``: the gpi macro's, else the file's own).
Cartesian q exists only where the spectrum's scan has a lattice epoch. Read-only.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

ACCEPTED = ("gpi", "header", "filename", "reported")     # the source words that make an analyzer's hkl accepted
INVALID_CELL = "the recorded lattice epoch is not a valid cell"


@dataclass(frozen=True)
class QPoint:
    hkl: tuple[float, float, float]              # rlu, the accepted per-analyzer hkl
    source: str                                  # "gpi" | "header" | "filename" | "reported"
    cart: tuple[float, float, float] | None      # inverse angstrom, crystal-frame Cartesian, ONLY when its scan has a lattice epoch; else None
    epoch: str | None                            # the epoch used for this point's scan ("lattice epoch v3"); None when cart is None


@dataclass(frozen=True)
class QCoordinates:
    points: dict[str, QPoint]                    # spectrum key -> point; only spectra with an accepted hkl
    reasons: dict[str, str]                      # every requested key without a point -> why
    lattices: dict[str, tuple[tuple[float, float, float, float, float, float], str]]   # scan -> ((a, b, c, alpha, beta, gamma), epoch source)


def _cell(epoch: dict) -> tuple[float, float, float, float, float, float] | None:
    """The epoch's cell, or None when it is not one (a length <= 0, an angle outside (0, 180), a volume <= 0)."""
    try:
        cell = tuple(float(epoch["lattice"][k]) for k in ("a", "b", "c", "alpha", "beta", "gamma"))
    except (KeyError, TypeError, ValueError):
        return None
    a, b, c, *angles = cell
    if not all(math.isfinite(x) for x in cell) or min(a, b, c) <= 0 or not all(0 < x < 180 for x in angles):
        return None
    ca, cb, cg = (math.cos(math.radians(x)) for x in angles)
    return cell if 1 - ca * ca - cb * cb - cg * cg + 2 * ca * cb * cg > 0 else None


def q_coordinates(root, keys: Collection[str]) -> QCoordinates:
    """The point of every requested spectrum key that has an accepted hkl; every other key gets its reason.

    The epoch is resolved per scan (``resolve_epoch`` by the scan's ``scan_number``): a scan with no epoch gives
    points with ``cart None`` and no reason; a scan whose epoch is not a valid cell gives no points at all
    (``INVALID_CELL``), never a silent hkl fallback. An unknown key says "unknown spectrum".
    """
    from isar.geometry.convert import hkl_to_q
    from isar.geometry.epochs import resolve_epoch
    from isar.geometry.sample_state import SampleState
    from isar.intake import gpi, inputs, scans as intake_scans, store
    from isar.session.workspace import Workspace

    root = Path(root)
    card, session = Workspace.open(root).load()
    rows = intake_scans.read_scans(root)                 # {} outside BL43: every key then has no accepted hkl
    index = gpi_now = None
    if rows:
        read = store.read_all(root, store.ENTRIES)
        index, gpi_now = inputs.index_entries(read.entries, lines=read.lines), gpi.entries_in_force(root)
    epochs = list(getattr(card.state, "lattice_epochs", None) or [])
    points: dict[str, QPoint] = {}
    reasons: dict[str, str] = {}
    lattices, scan_epoch, scan_inputs = {}, {}, {}
    for key in keys:
        entry = session.scans.get(key)
        if entry is None:
            reasons[key] = "unknown spectrum"
            continue
        scan, det = key.rsplit(":", 1)
        if scan not in scan_epoch:
            ep = resolve_epoch(epochs, scan_ref=str(entry.scan_number))
            cell = None if ep is None else _cell(ep)
            if ep is not None and cell is None:
                log.warning("q provider: the lattice epoch of %s is not a valid cell (%s); its spectra get no q",
                            scan, ep.get("lattice"))
            scan_epoch[scan] = (ep, cell)
            if cell is not None:
                lattices[scan] = (cell, ep.get("source"))
        ep, cell = scan_epoch[scan]
        if ep is not None and cell is None:
            reasons[key] = INVALID_CELL
            continue
        row = rows.get(scan)
        if row is None:
            q, source = None, "none"
        else:
            if scan not in scan_inputs:
                scan_inputs[scan] = inputs.scan_inputs(row, index, gpi_now.get(scan))
            q, source = inputs.analyzer_q(row, scan_inputs[scan], det)
        if q is None or source not in ACCEPTED:
            reasons[key] = "no accepted hkl"
            continue
        cart = epoch = None
        if cell is not None:
            cart = tuple(hkl_to_q(SampleState(*cell), *q)["q_inv_angstrom"])
            epoch = f"lattice epoch v{ep.get('version')}"
        points[key] = QPoint(hkl=tuple(float(x) for x in q), source=source, cart=cart, epoch=epoch)
    return QCoordinates(points=points, reasons=reasons, lattices=lattices)
