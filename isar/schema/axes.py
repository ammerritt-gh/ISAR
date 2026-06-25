"""Scan-axis descriptors — the generalization that makes constant-Q-energy a special case (§5.2).

A scan is a list of axis descriptors `{name, kind, role, units, values[nP]}`. Phase 2 populates
only the constant-Q-energy shape: exactly one `scanned` axis of `kind="energy"` plus three
`constant` Q axes. The list exists so a constant-E scan, a rocking curve, or a ramp is a different
*population* of the same container, never a schema change (§5.6, D7).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

AXIS_KINDS = ("energy", "q_h", "q_k", "q_l", "q_modulus", "angle", "environment")
AXIS_ROLES = ("scanned", "derived", "constant")


@dataclass
class ScanAxis:
    name: str
    kind: str
    role: str
    units: str
    values: np.ndarray

    _ARRAY_FIELDS = {"values": "float64"}


def energy_axis(values, name: str = "energy", units: str = "meV") -> ScanAxis:
    """The single scanned energy-transfer axis of a constant-Q-energy scan."""
    return ScanAxis(name=name, kind="energy", role="scanned", units=units,
                    values=np.asarray(values, dtype="float64"))


def constant_q_axes(h: float, k: float, l: float, n_points: int) -> list[ScanAxis]:
    """The nominal Q of a constant-Q scan, recorded as three constant axes of length nP."""
    n = int(n_points)
    return [
        ScanAxis("q_h", "q_h", "constant", "rlu", np.full(n, float(h))),
        ScanAxis("q_k", "q_k", "constant", "rlu", np.full(n, float(k))),
        ScanAxis("q_l", "q_l", "constant", "rlu", np.full(n, float(l))),
    ]


def scanned_axes(record) -> list[ScanAxis]:
    return [a for a in record.scan_axes if a.role == "scanned"]


def assert_constant_q_energy(record) -> None:
    """Guard: Phase 2 only ever produces one scanned energy axis (§5.6). Refuse anything else."""
    sc = scanned_axes(record)
    if len(sc) != 1 or sc[0].kind != "energy":
        kinds = [(a.name, a.kind) for a in sc]
        raise ValueError(
            f"expected exactly one scanned energy axis (constant-Q-energy, §5.6); got {kinds}"
        )
