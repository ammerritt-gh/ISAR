"""isar.geometry — stateless metric / UB / feasibility layer (Qt-free, numpy-only).

Vendored from two sibling repos (TAVI, GPL-3, and TAS_MCP, same author as ISAR) and adapted;
see the provenance header on each module. This package imports ONLY stdlib + numpy — no Qt,
matplotlib, scipy, mcp, tavi, or isar.analyze/drive/session internals — so the GUI core and
the planner can read it freely.

Frame convention
----------------
The public functions work in the INSTRUMENT frame: the scattering plane is horizontal and
the +z instrument axis is vertical / out-of-plane (this is what ``plane.set_orientation_from_plane``
and TAVI's ``get_scattering_plane_info`` use). ``tas_geometry`` additionally carries the
mounted-component (McStas) frame — x, z horizontal; y vertical — and bridges the two with
``component_q_to_instrument_q`` / ``instrument_q_to_component_q``. The bridge is pinned by
``tests/geometry/test_frame_convention.py``.

Statelessness
-------------
Every public function takes the lattice/orientation as a per-call ``SampleState`` (or raw
lattice) argument. There is no module- or session-level mutable state: a lattice/orientation
interpretation is a *versioned epoch* (see ``epochs``), because it can change mid-experiment
and be corrected retroactively.

Feasibility here is a PREVIEW; TAVI's HTTP /validate remains authoritative.
"""
from __future__ import annotations

from .convert import hkl_to_q, q_to_hkl, d_spacing, bragg_two_theta
from .plane import (
    set_orientation_from_plane,
    scattering_plane_info,
    scattering_plane_check,
)
from .feasibility import check_feasibility, accessible_range
from .ub_matrix import compute_B_matrix, UBMatrix
from .sample_mount import reciprocal_basis_tas
from .sample_state import SampleState
from .epochs import resolve_epoch, sample_state_from_epoch

__all__ = [
    "hkl_to_q",
    "q_to_hkl",
    "d_spacing",
    "bragg_two_theta",
    "set_orientation_from_plane",
    "scattering_plane_info",
    "scattering_plane_check",
    "check_feasibility",
    "accessible_range",
    "compute_B_matrix",
    "UBMatrix",
    "reciprocal_basis_tas",
    "SampleState",
    "resolve_epoch",
    "sample_state_from_epoch",
]
