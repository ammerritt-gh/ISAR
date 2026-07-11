# Vendored from TAS_MCP (user's own repo, no LICENSE file; same author as ISAR),
# original path tas_mcp/session.py.  Vendored 2026-07-11, adapted: only the SampleState value
# object is kept — the stateful TASSession, the instrument/fixed-k session config, and the MCP
# server layer are dropped. Sample orientation still delegates to the vendored UBMatrix.
"""Sample value object: lattice parameters plus an optional UB orientation.

``SampleState`` is the per-call value object the stateless geometry functions
(``convert``, ``plane``, ``feasibility``) accept. It carries no instrument or scan
configuration — those are passed as explicit arguments — so a single lattice/orientation
interpretation can be threaded through a computation without any module- or session-level
mutable state.

Sample orientation is delegated to ``isar.geometry.ub_matrix.UBMatrix`` so HKL<->Q with a
mounted crystal uses exactly the same Busing-Levy math as TAVI itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .ub_matrix import UBMatrix


@dataclass
class SampleState:
    """Lattice parameters plus an optional UB orientation."""

    a: float
    b: float
    c: float
    alpha: float
    beta: float
    gamma: float
    _ub: Optional[UBMatrix] = None
    orientation_set: bool = False

    def ub(self) -> UBMatrix:
        """Return a UBMatrix for this sample, constructing one if needed."""
        if self._ub is None:
            self._ub = UBMatrix(self.a, self.b, self.c, self.alpha, self.beta, self.gamma)
        return self._ub

    @property
    def oriented(self) -> bool:
        """True if an orientation was deliberately established (peaks or plane).

        This is an explicit flag rather than a U != identity test, because a
        legitimately-aligned crystal can have U == identity yet still be oriented.
        """
        return self.orientation_set

    @property
    def lattice(self) -> tuple:
        return (self.a, self.b, self.c, self.alpha, self.beta, self.gamma)
