"""Canonical uniform scan-grid arithmetic, shared by candidate design and request emission.

One module owns *how many points a window has*, *where those points sit*, and *how the window is
rendered into the instrument command*.  Two implementations of that arithmetic — one in the scan
designer and one at submission — would let a scored design and the executed request disagree while
both look correct in isolation.  This module is that single seam.

All three answers are delegated to an **instrument grid adapter**
(:mod:`isar.drive.acquisition.grid_adapters`): the instrument, not ISAR, decides how many points a
``deltaE lo hi step`` command has and at what precision they are executed.  The grid fingerprints a
later scoring/selection stage compares are therefore taken over the *adapter-canonical executed*
energies — the points the instrument will actually visit — never over raw binary floats and never
over a precision ISAR invented for itself.  The adapter identity travels with the fingerprint so a
stored candidate declares which instrument semantics it was scored under.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from isar.drive.acquisition.grid_adapters import (
    DEFAULT_GRID_ADAPTER,
    TaviUniformGridAdapter,
)
from isar.drive.acquisition.inventory import canonical_json, json_number


MAX_POINTS = 100
UNIFORM_SCAN_GRID_SCHEMA = "isar.uniform_scan_grid/2"


def resolve_point_count(e_lo: float, e_hi: float, e_step: float, *,
                        adapter: TaviUniformGridAdapter = DEFAULT_GRID_ADAPTER) -> tuple[int, float]:
    """``(point count, sanitized step)`` for one window — the single counting rule.

    Both the budget-side ledger helper and the grid resolver go through here, so an unbudgeted
    request and the grid it resolves to cannot disagree about how many points the instrument will
    execute.  Two degenerate inputs are normalized before the adapter sees them, identically for both
    callers: a non-positive step becomes ``1e-9`` (the instrument would divide by zero), and an
    inverted window counts as its single starting point rather than a negative number of points.
    """
    step = max(float(e_step), 1e-9)
    lo, hi = float(e_lo), float(e_hi)
    return adapter.point_count(lo, max(hi, lo), step), step


def points_in_window(e_lo: float, e_hi: float, e_step: float, *,
                     max_points: int = MAX_POINTS,
                     adapter: TaviUniformGridAdapter = DEFAULT_GRID_ADAPTER) -> int:
    """Number of instrument scan points, capped by the caller's frozen policy."""
    count, _ = resolve_point_count(e_lo, e_hi, e_step, adapter=adapter)
    return min(count, max(int(max_points), 2))


def format_scan_command(e_lo: float, e_hi: float, e_step: float, *,
                        adapter: TaviUniformGridAdapter = DEFAULT_GRID_ADAPTER) -> str:
    """The instrument ``scan_command1`` rendering for this window.

    Canonical numeric rendering for the emitted request: the ``%g`` form is the one ISAR has always
    submitted, and it stays owned by the adapter so the designer, the submitter, and the executed-grid
    prediction cannot drift apart.
    """
    return adapter.format_scan_command(e_lo, e_hi, e_step)


@dataclass(frozen=True)
class UniformGrid:
    """One resolved uniform energy window: bounds, step, point count, and its executed points."""

    e_lo: float
    e_hi: float
    e_step: float
    n_points: int
    adapter: TaviUniformGridAdapter = field(default=DEFAULT_GRID_ADAPTER)

    @property
    def energies(self) -> tuple[float, ...]:
        """The ordered points the instrument executes for this window."""
        return self.adapter.executed_energies(self.e_lo, self.e_step, self.n_points)

    def scan_command(self) -> str:
        return self.adapter.format_scan_command(self.e_lo, self.e_hi, self.e_step)

    def support_points(self, band: tuple[float, float] | None,
                       sigma_multiple: float = 2.0) -> int | None:
        """Grid points inside ``mean ± sigma_multiple·sigma``; ``None`` without a declared band.

        Mirrors the loop's target-support rule (:func:`isar.drive.loop._target_supporting_feasible_points`)
        at design time, where no feasibility manifest exists yet.  Counted over the *executed*
        energies: support the instrument will not actually measure is not support.
        """
        if band is None:
            return None
        mean, sigma = float(band[0]), float(band[1])
        lo = mean - float(sigma_multiple) * sigma
        hi = mean + float(sigma_multiple) * sigma
        return sum(1 for energy in self.energies if lo <= energy <= hi)

    def fingerprint(self, ncount: int) -> str:
        """Content address of ``(executed grid, per-point exposure, adapter)`` — the scoring identity.

        The adapter identity is inside the hash on purpose: the same ``(lo, hi, step)`` executed under
        different instrument semantics is a different measurement, and a score is only valid for the
        semantics it was computed under.
        """
        payload = canonical_json({
            "grid_schema": UNIFORM_SCAN_GRID_SCHEMA,
            "adapter": self.adapter.identity(),
            # Through the shared non-finite-safe rendering: a degenerate window must fingerprint to
            # a stable address that records its degeneracy, not raise out of the designer.
            "energies": [json_number(value) for value in self.energies],
            "ncount": int(ncount),
        })
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_uniform_grid(e_lo: float, e_hi: float, e_step: float, *,
                         max_points: int = MAX_POINTS,
                         adapter: TaviUniformGridAdapter = DEFAULT_GRID_ADAPTER) -> UniformGrid:
    """Resolve a window into an executable grid, coarsening the step only at the point cap.

    The point count is the adapter's — i.e. the instrument's — answer for the command ISAR would
    submit, not an independent ISAR rule that happens to agree.  When it exceeds the cap the step is
    widened to land exactly ``max_points`` points across the same span, and the count is re-asked of
    the adapter so the wire rendering of the coarsened step cannot smuggle an extra point past the
    cap.
    """
    e_lo, e_hi = float(e_lo), float(e_hi)
    span = e_hi - e_lo
    point_cap = max(int(max_points), 2)
    n_points, step = resolve_point_count(e_lo, e_hi, e_step, adapter=adapter)
    if n_points > point_cap:
        step = span / (point_cap - 1)
        n_points, step = resolve_point_count(e_lo, e_hi, step, adapter=adapter)
    return UniformGrid(e_lo=e_lo, e_hi=e_hi, e_step=step, n_points=n_points, adapter=adapter)


__all__ = [
    "MAX_POINTS",
    "UNIFORM_SCAN_GRID_SCHEMA",
    "UniformGrid",
    "format_scan_command",
    "points_in_window",
    "resolve_point_count",
    "resolve_uniform_grid",
]
