"""Hand-made spectra, coordinates and graphs for the inter-spectra graph tests: no workspace, no files.

A top-level helper beside ``tests/inter_fixtures.py``, imported the same way (``tests/`` is on ``sys.path``).
Coordinates are synthetic: q = (col * 0.1, row * 0.1, 0) rlu on the BL43 grid unless a test says otherwise.
"""
from __future__ import annotations

from isar.session.inter_spectra.graph import build_graph
from isar.session.inter_spectra.layout import BL43_GRID
from isar.session.inter_spectra.nodes import NodeSet, SpectrumNode
from isar.session.inter_spectra.q_provider import QPoint

SCAN = "E:10"
DETS = tuple(d for row in BL43_GRID.rows for d in row)


def node(scan, det, *, series=None, T=None, label="Smp", eligible=True, reason=None) -> SpectrumNode:
    return SpectrumNode(f"{scan}:{det}", scan, det, label, series, T, eligible, reason)


def point(hkl, cart=None, epoch=None) -> QPoint:
    return QPoint(hkl=tuple(float(x) for x in hkl), source="gpi", cart=cart, epoch=epoch)


def scan_nodes(scan=SCAN, dets=DETS, **kw) -> dict:
    return {f"{scan}:{d}": node(scan, d, **kw) for d in dets}


def grid_coords(scan=SCAN, dets=DETS) -> dict:
    return {f"{scan}:{d}": point((c * 0.1, r * 0.1, 0.0))
            for r, row in enumerate(BL43_GRID.rows) for c, d in enumerate(row) if d in dets}


def build(nodes, ns=None, *, scope=None, kinds=("q",), coords=None, tol=1e-6, grid=BL43_GRID):
    ns = ns or NodeSet(nodes=nodes, series={}, slots={}, repeats=())
    return build_graph(nodes, ns, scope=set(nodes) if scope is None else set(scope), kinds=frozenset(kinds),
                       grid=grid, coords=coords or {}, collinearity_tol=tol)
