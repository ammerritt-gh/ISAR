"""Effective hkl and coordinates per spectrum for the graph (C-q-provider). Stdlib only.

S1 has no hkl provider: ``q_coordinates`` yields no points, and every requested spectrum says "no accepted
hkl". S2U1 fills the provider from the accepted per-analyzer hkl and the scan's lattice epoch.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass


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


def q_coordinates(root, keys: Collection[str]) -> QCoordinates:
    # shortcut: S1 has no hkl provider (S2U1 fills it), so no spectrum has a point; each key says why
    return QCoordinates(points={}, reasons={k: "no accepted hkl" for k in keys}, lattices={})
