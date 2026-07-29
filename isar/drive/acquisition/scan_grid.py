"""Canonical uniform scan-grid arithmetic, shared by candidate design and request emission.

One module owns *how many points a window has*, *where those points sit*, and *how the window is
rendered into the instrument command*.  Two implementations of that arithmetic — one in the scan
designer and one at submission — would let a scored design and the executed request disagree while
both look correct in isolation.  This module is that single seam; the grid fingerprints a later
scoring/selection stage compares are taken over :func:`canonical_energies`, never over raw binary
floats.

TAVI's ``deltaE <lo> <hi> <step>`` semantics are *inclusive*: the scan visits ``lo``, ``lo + step``,
… up to and including the last point that reaches ``hi`` under the nearest-integer point rule below.
The rule here is the one ISAR has always emitted (it is what the legacy window arithmetic computed);
it is restated as a named contract so it can be tested without a TAVI runtime dependency.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from isar.drive.acquisition.inventory import canonical_json


MAX_POINTS = 100
#: Energies are compared and fingerprinted at this decimal precision (1e-6 meV), never as raw
#: binary floats: two constructions that agree physically must produce one grid identity.
GRID_ENERGY_DECIMALS = 6
UNIFORM_SCAN_GRID_SCHEMA = "isar.uniform_scan_grid/1"


def points_in_window(e_lo: float, e_hi: float, e_step: float, *,
                     max_points: int = MAX_POINTS) -> int:
    """Number of TAVI scan points, capped by the caller's frozen policy."""
    step = max(float(e_step), 1e-9)
    span = max(float(e_hi) - float(e_lo), 0.0)
    return min(int(math.floor(span / step + 0.5)) + 1, max(int(max_points), 2))


def canonical_energy(value: float) -> float:
    """One energy in canonical grid-identity form (rounded, no negative zero)."""
    rounded = round(float(value), GRID_ENERGY_DECIMALS)
    return rounded + 0.0


def canonical_energies(e_lo: float, e_step: float, n_points: int) -> tuple[float, ...]:
    """The ordered inclusive point grid ``e_lo + i·e_step`` in canonical form."""
    count = max(int(n_points), 0)
    return tuple(canonical_energy(float(e_lo) + index * float(e_step))
                 for index in range(count))


def format_scan_command(e_lo: float, e_hi: float, e_step: float) -> str:
    """The TAVI ``scan_command1`` rendering for this window.

    Canonical numeric rendering for the emitted request: the ``%g`` form is the one ISAR has always
    submitted, and it stays here so the designer and the submitter cannot drift apart.
    """
    return f"deltaE {float(e_lo):g} {float(e_hi):g} {float(e_step):g}"


@dataclass(frozen=True)
class UniformGrid:
    """One resolved uniform energy window: bounds, step, point count, and its canonical points."""

    e_lo: float
    e_hi: float
    e_step: float
    n_points: int

    @property
    def energies(self) -> tuple[float, ...]:
        return canonical_energies(self.e_lo, self.e_step, self.n_points)

    def scan_command(self) -> str:
        return format_scan_command(self.e_lo, self.e_hi, self.e_step)

    def support_points(self, band: tuple[float, float] | None,
                       sigma_multiple: float = 2.0) -> int | None:
        """Grid points inside ``mean ± sigma_multiple·sigma``; ``None`` without a declared band.

        Mirrors the loop's target-support rule (:func:`isar.drive.loop._target_supporting_feasible_points`)
        at design time, where no feasibility manifest exists yet.
        """
        if band is None:
            return None
        mean, sigma = float(band[0]), float(band[1])
        lo = mean - float(sigma_multiple) * sigma
        hi = mean + float(sigma_multiple) * sigma
        return sum(1 for energy in self.energies if lo <= energy <= hi)

    def fingerprint(self, ncount: int) -> str:
        """Content address of ``(canonical grid, per-point exposure)`` — the scoring identity."""
        payload = canonical_json({
            "grid_schema": UNIFORM_SCAN_GRID_SCHEMA,
            "energies": list(self.energies),
            "ncount": int(ncount),
        })
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_uniform_grid(e_lo: float, e_hi: float, e_step: float, *,
                         max_points: int = MAX_POINTS) -> UniformGrid:
    """Resolve a window into an executable grid, coarsening the step only at the point cap.

    This is the exact arithmetic the legacy window builders end with: nearest-integer inclusive point
    count, and — when that exceeds the cap — a step widened to land exactly ``max_points`` points
    across the same span.
    """
    e_lo, e_hi = float(e_lo), float(e_hi)
    step = max(float(e_step), 1e-9)
    span = e_hi - e_lo
    point_cap = max(int(max_points), 2)
    n_points = int(math.floor(span / step + 0.5)) + 1
    if n_points > point_cap:
        step = span / (point_cap - 1)
        n_points = point_cap
    return UniformGrid(e_lo=e_lo, e_hi=e_hi, e_step=step, n_points=n_points)


__all__ = [
    "GRID_ENERGY_DECIMALS",
    "MAX_POINTS",
    "UNIFORM_SCAN_GRID_SCHEMA",
    "UniformGrid",
    "canonical_energies",
    "canonical_energy",
    "format_scan_command",
    "points_in_window",
    "resolve_uniform_grid",
]
