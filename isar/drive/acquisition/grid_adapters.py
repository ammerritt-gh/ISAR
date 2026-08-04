"""Instrument grid adapters: what the instrument *actually executes* for a uniform scan request.

ISAR designs a uniform window as ``(e_lo, e_hi, e_step)``.  The instrument does not execute that
triple — it executes the point list its own parser derives from the submitted command string, at its
own numeric precision.  Scoring a design against energies the instrument will never visit is a silent
correctness bug: the scored grid and the executed grid look equally plausible in isolation.

This module is the seam that closes that gap:

    uniform request → adapter-canonical executed point list → scoring grid + fingerprint

An adapter is *the* authority on three things for one instrument: how the request is rendered onto
the wire, how many points that rendering produces, and where those points sit.  Every grid identity
(candidate fingerprints, dedup keys, target-support counting) is taken over the adapter-canonical
executed energies, and the adapter's ``id``/``version`` are recorded in grid provenance so a stored
candidate says which instrument semantics it was scored under.

The TAVI adapter reproduces ``tavi.utilities.parse_scan_steps`` exactly (verified against TAVI
``tavi/utilities.py:91-118``) **without importing TAVI**: ISAR must not grow a runtime dependency on
the instrument server to know what it asked for.  Cross-repo agreement is held by the golden parity
fixtures in ``tests/drive/test_grid_adapters.py`` plus one live round-trip test.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


#: Schema of the adapter-identity block persisted in grid provenance.
GRID_ADAPTER_SCHEMA = "isar.instrument_grid_adapter/1"


@dataclass(frozen=True)
class TaviUniformGridAdapter:
    """TAVI's ``deltaE <lo> <hi> <step>`` execution semantics.

    TAVI receives the command as a *string* and parses it back with ``float()``, so the wire
    rendering is part of the contract: a designed bound of ``3.3999999999999995`` reaches the
    instrument as ``3.4``.  The adapter therefore round-trips every value through the same ``%g``
    rendering ISAR submits before applying TAVI's arithmetic:

    * ``num_steps = int(np.floor((end - start) / step + 0.5)) + 1``
    * ``last = start + step * (num_steps - 1)``
    * ``values = np.round(np.linspace(start, last, num_steps), 3)``

    ``np.round`` is **numpy's** round-half-to-even, not Python's ``round`` — a value landing exactly
    on ``x.xxx5`` goes to the even neighbour.  Reimplementing this with :func:`round` would disagree
    on those ties, which is why the rounding here goes through numpy.
    """

    adapter_id: str = "tavi_delta_e"
    adapter_version: int = 1
    #: Command axis name; the first token of the submitted ``scan_command1``.
    axis: str = "deltaE"
    #: Decimal places TAVI rounds its executed scan values to (``np.round(..., 3)``).
    executed_decimals: int = 3

    # -- identity ---------------------------------------------------------------------------------
    def identity(self) -> dict:
        """The provenance block recorded with every grid fingerprint."""
        return {
            "adapter_schema": GRID_ADAPTER_SCHEMA,
            "adapter_id": self.adapter_id,
            "adapter_version": int(self.adapter_version),
        }

    # -- wire rendering ---------------------------------------------------------------------------
    def format_scan_command(self, e_lo: float, e_hi: float, e_step: float) -> str:
        """The ``scan_command1`` string ISAR submits for this window.

        The ``%g`` form is the one ISAR has always submitted; it stays byte-identical here so the
        adapter changes the scoring *representation* of a window, never the request.
        """
        return f"{self.axis} {float(e_lo):g} {float(e_hi):g} {float(e_step):g}"

    def wire_value(self, value: float) -> float:
        """One request number as the instrument parses it back off the wire."""
        return float(f"{float(value):g}")

    def wire_window(self, e_lo: float, e_hi: float,
                    e_step: float) -> tuple[float, float, float]:
        """``(start, end, step)`` as ``parse_scan_steps`` sees them, after the ``%g`` round trip."""
        return self.wire_value(e_lo), self.wire_value(e_hi), self.wire_value(e_step)

    # -- executed grid ----------------------------------------------------------------------------
    def point_count(self, e_lo: float, e_hi: float, e_step: float) -> int:
        """How many points the instrument executes for this request.

        TAVI's nearest-integer inclusive rule, evaluated on the wire values.  Non-finite inputs raise
        ``OverflowError``/``ValueError`` out of ``int()`` exactly as they do inside TAVI; callers that
        can see degenerate instrument state catch that and record a structured refusal.
        """
        start, end, step = self.wire_window(e_lo, e_hi, e_step)
        return int(np.floor((end - start) / step + 0.5)) + 1

    def executed_energies(self, e_lo: float, e_step: float,
                          n_points: int) -> tuple[float, ...]:
        """The ordered point list the instrument visits, at its own execution precision.

        Takes the resolved point count rather than re-deriving it so a degenerate window cannot make
        this allocate an unbounded array; :meth:`point_count` owns the counting rule.

        The single-point case returns ``e_lo`` itself rather than TAVI's ``linspace(start, start +
        step·0, 1)``.  That product is NaN for a degenerate (infinite) step, and an audit record
        naming a NaN energy is strictly worse than one naming the only bound the request declares —
        this is a deliberate deviation in ISAR's *representation* of a request the instrument would
        never be asked to run.
        """
        count = max(int(n_points), 0)
        if count == 0:
            return ()
        start = self.wire_value(e_lo)
        if count == 1:
            return (float(np.round(start, self.executed_decimals)) + 0.0,)
        step = self.wire_value(e_step)
        last = start + step * (count - 1)
        values = np.round(np.linspace(start, last, count), self.executed_decimals)
        # ``+ 0.0`` normalizes negative zero: two constructions that agree physically must not
        # fingerprint differently over ``-0.0`` versus ``0.0``.
        return tuple(float(value) + 0.0 for value in values)

    def canonicalize_returned_energies(self, values: Sequence[float]) -> tuple[float, ...]:
        """Validate and canonicalize TAVI's returned executed-energy values.

        JSON float representation noise is accepted, but a genuinely off-contract instrument value
        is not laundered by rounding.  Each input must already be within a tiny serialization
        tolerance of TAVI's declared executed precision before the canonical tuple is returned.
        """
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ValueError("returned executed grid must be a numeric sequence")
        canonical: list[float] = []
        for value in values:
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(float(value))):
                raise ValueError("returned executed grid contains a non-finite numeric value")
            number = float(value)
            rounded = float(np.round(number, self.executed_decimals)) + 0.0
            tolerance = 1e-12 * max(1.0, abs(rounded))
            if abs(number - rounded) > tolerance:
                raise ValueError("returned executed grid is outside adapter precision")
            canonical.append(rounded)
        return tuple(canonical)


#: The instrument ISAR drives.  A second adapter declares its own id/version and is threaded through
#: :func:`isar.drive.acquisition.scan_grid.resolve_uniform_grid`; nothing here is TAVI-specific
#: beyond this instance.
TAVI_GRID_ADAPTER = TaviUniformGridAdapter()

#: The adapter every scan-design path uses until an instrument seam supplies another one.
DEFAULT_GRID_ADAPTER = TAVI_GRID_ADAPTER


__all__ = [
    "DEFAULT_GRID_ADAPTER",
    "GRID_ADAPTER_SCHEMA",
    "TAVI_GRID_ADAPTER",
    "TaviUniformGridAdapter",
]
