# Vendored from TAS_MCP (user's own repo, no LICENSE file; same author as ISAR),
# original path tas_mcp/session.py.  Vendored 2026-07-11, adapted: only the SampleState value
# object is kept — the stateful TASSession, the instrument/fixed-k session config, and the MCP
# server layer are dropped. Sample orientation still delegates to the vendored UBMatrix.
# 2026-07-12: `ub()` cache invalidates when the lattice is mutated (rebuilds B, preserves U).
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

from dataclasses import dataclass, field
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
    # The lattice tuple the cached ``_ub`` was built from; used to detect stale caches when
    # the lattice fields are mutated after ``ub()`` first built the UBMatrix. Not part of the
    # value-object identity (compare/repr off).
    _ub_lattice: Optional[tuple] = field(default=None, compare=False, repr=False)

    def ub(self) -> UBMatrix:
        """Return a UBMatrix for this sample, constructing one if needed.

        The UBMatrix is cached, but the cache is keyed on the lattice it was built from. If
        the lattice fields are mutated after the first build the old B is invalid, so a fresh
        UBMatrix is rebuilt from the new lattice. The mount rotation U is lattice-independent,
        so an established orientation is carried over onto the rebuilt matrix — an oriented
        sample stays oriented across a lattice change.
        """
        if self._ub is None:
            self._ub = UBMatrix(self.a, self.b, self.c, self.alpha, self.beta, self.gamma)
            self._ub_lattice = self.lattice
        elif self.lattice != self._ub_lattice:
            old_u = self._ub.U
            rebuilt = UBMatrix(self.a, self.b, self.c, self.alpha, self.beta, self.gamma)
            if self.orientation_set:
                rebuilt.set_U(old_u)
            self._ub = rebuilt
            self._ub_lattice = self.lattice
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
