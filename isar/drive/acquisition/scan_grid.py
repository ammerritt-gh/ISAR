"""Canonical uniform scan-grid arithmetic, shared by candidate design and request emission.

One module owns *how many points a window has*, *where those points sit*, and *how the window is
rendered into the instrument command*.  Two implementations of that arithmetic — one in the scan
designer and one at submission — would let a scored design and the executed request disagree while
both look correct in isolation.  This module is that single seam.

Both answers are delegated to an **instrument grid adapter**
(:mod:`isar.drive.acquisition.grid_adapters`): the instrument, not ISAR, decides how many points a
``deltaE lo hi step`` command has and at what precision they are executed.  The grid fingerprints a
later scoring/selection stage compares are therefore taken over the *adapter-canonical executed*
energies — the points the instrument will actually visit — never over raw binary floats and never
over a precision ISAR invented for itself.  The adapter identity travels with the fingerprint so a
stored candidate declares which instrument semantics it was scored under.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from isar.drive.acquisition.grid_adapters import (
    DEFAULT_GRID_ADAPTER,
    TaviUniformGridAdapter,
)


#: Point ceiling used when a caller supplies no policy cap; kept equal to
#: ``SteeringPolicy.max_scan_points`` so a window built without a policy is not clamped harder than
#: one built with the default policy. See that field for why 100 no longer covers a survey band.
MAX_POINTS = 220

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


def snap_to_lattice(e_lo: float, e_hi: float, e_step: float, *,
                    adapter: TaviUniformGridAdapter = DEFAULT_GRID_ADAPTER
                    ) -> tuple[float, float, float]:
    """Quantise one designed window onto the instrument's SHARED energy lattice.

    Two scans at the same (sample, Q, T) only add counts to a pooled master when their executed
    points coincide: :func:`isar.reduce.collate._merge_group` sums counts and monitors per energy
    BIN, and its binning tolerance is a quarter of the median spacing of the members' concatenated
    energies.  Windows that differ by a fraction of a step therefore do not merely bin coarsely --
    they drive that tolerance down until every member owns its own bins, so the master's
    ``n_members`` grows while its per-bin exposure does not.  Measured on the 2026-09-03 full-path
    campaign: 65 members at h = 2.90 produced 1419 bins at 0.001 meV median spacing, a summed
    monitor of 1.4e10 spread so thinly that the peak-local median (which is what
    ``effective_exposure`` reports) stayed at one scan's 1e7.

    The lattice is the instrument's own: the step is quantised to the adapter's executed precision,
    and the bounds to whole multiples of that step anchored at zero.  Bounds move OUTWARD (``e_lo``
    down, ``e_hi`` up) so the emitted window never trims the band the design asked for; it grows by
    at most one point per end, and :func:`resolve_uniform_grid` still owns the point cap.
    """
    lo, hi, raw_step = float(e_lo), float(e_hi), float(e_step)
    if not all(math.isfinite(v) for v in (lo, hi, raw_step)) or raw_step <= 0.0:
        return lo, hi, raw_step
    places = int(adapter.executed_decimals)
    quantum = 10.0 ** -places
    step = max(round(raw_step / quantum) * quantum, quantum)
    # The tolerance absorbs binary-float error in ``bound / step`` so a bound already ON the lattice
    # is not pushed a whole point outward (2.28 / 0.19 evaluates to 11.999999999999998).
    lo_k = math.floor(lo / step + 1e-9)
    hi_k = math.ceil(max(hi, lo) / step - 1e-9)
    return round(lo_k * step, places), round(hi_k * step, places), round(step, places)


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


def resolve_uniform_grid(e_lo: float, e_hi: float, e_step: float, *,
                         max_points: int = MAX_POINTS,
                         adapter: TaviUniformGridAdapter = DEFAULT_GRID_ADAPTER) -> UniformGrid:
    """Resolve a window into an executable grid, coarsening the step only at the point cap.

    The point count is the adapter's — i.e. the instrument's — answer for the command ISAR would
    submit, not an independent ISAR rule that happens to agree.  When it exceeds the cap the step is
    widened to land exactly ``max_points`` points across the same span, and the count is re-asked of
    the adapter so the wire rendering of the coarsened step cannot smuggle an extra point past the
    cap.

    Re-asking makes the count *honest*, which is not the same as making it fit: the coarsened step
    must be derived from the span the adapter will actually count over — the **wire** span, after the
    ``%g`` round trip — not the raw designed span.  Dividing the raw span by ``point_cap - 1`` yields
    a step fractionally too small whenever the rendering moves a bound, and the adapter then answers
    ``point_cap + 1``.  Clamping ``n_points`` instead would be exactly the bug ADR-018 exists to
    remove: the count must stay the instrument's answer, never ISAR's preferred one.

    The residual error is then only ``%g``'s own rendering of the step, which cannot cross the
    adapter's ``0.5``-point rounding margin below a cap around ``10**6`` — six orders above TAVI's
    200-point ceiling.  Past that the wire protocol has too few significant digits to express a
    conforming step at all, and no arithmetic here can recover it.
    """
    e_lo, e_hi = float(e_lo), float(e_hi)
    point_cap = max(int(max_points), 2)
    n_points, step = resolve_point_count(e_lo, e_hi, e_step, adapter=adapter)
    if n_points > point_cap:
        span_lo, span_hi, _ = adapter.wire_window(e_lo, max(e_hi, e_lo), step)
        step = (span_hi - span_lo) / (point_cap - 1)
        n_points, step = resolve_point_count(e_lo, e_hi, step, adapter=adapter)
    return UniformGrid(e_lo=e_lo, e_hi=e_hi, e_step=step, n_points=n_points, adapter=adapter)


__all__ = [
    "MAX_POINTS",
    "UniformGrid",
    "format_scan_command",
    "resolve_point_count",
    "resolve_uniform_grid",
    "snap_to_lattice",
]
