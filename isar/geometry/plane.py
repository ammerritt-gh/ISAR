# Vendored from TAS_MCP (user's own repo, no LICENSE file; same author as ISAR),
# original path tas_mcp/plane.py.  Vendored 2026-07-11, adapted: (1) the tavi import shim
# replaced with the vendored `.ub_matrix` / `.tas_geometry` primitives; (2) made stateless —
# functions take a `SampleState` per call; `set_orientation_from_plane` establishes U on the
# passed sample (the caller's per-call object) instead of on a stored session.
"""Scattering-plane analysis and orientation-from-plane.

Wraps ``get_scattering_plane_info`` and adds the convenience the callers actually need:
define the horizontal scattering plane by two reciprocal-lattice vectors (e.g. [1,0,0] and
[0,0,1] for an (H,0,L) plane), then ask whether a target reflection lies in that plane and
what its out-of-plane angle is. The chosen frame is the INSTRUMENT frame: the scattering
plane is horizontal and the +z instrument axis is vertical / out-of-plane.
"""
from __future__ import annotations

import math

import numpy as np

from .ub_matrix import get_scattering_plane_info
from .tas_geometry import component_q_to_instrument_q
from .sample_state import SampleState

_IN_PLANE_TOL_DEG = 0.05


def _require_oriented(sample: SampleState) -> SampleState:
    if sample is None:
        raise ValueError("No sample set. Configure a lattice first.")
    if not sample.oriented:
        raise ValueError(
            "Sample orientation not set. Use set_orientation_from_plane or "
            "set an orientation from peaks first."
        )
    return sample


def set_orientation_from_plane(sample: SampleState, in_plane_1, in_plane_2) -> dict:
    """Orient the crystal so the two given hkl vectors span the horizontal plane.

    Builds U directly in the mounted-sample (component) convention, where the scattering
    plane is the xz-plane and the +y axis is vertical / out-of-plane. The first hkl vector
    is placed along sample x, the plane normal along sample y, and their completion along
    sample z. This matches the frame that ``UBMatrix.hkl_to_q`` and
    ``get_scattering_plane_info`` use (qy = out of plane), so an in-plane reflection comes
    back with qy = 0.

    Mutates ``sample`` (sets its U and marks it oriented) and returns the resulting plane
    info. The sample is the caller's per-call value object, not shared session state.
    """
    if sample is None:
        raise ValueError("No sample set. Configure a lattice first.")
    ub = sample.ub()
    B = ub.B

    v1 = B @ np.asarray(in_plane_1, dtype=float)
    v2 = B @ np.asarray(in_plane_2, dtype=float)
    if np.linalg.norm(v1) < 1e-8 or np.linalg.norm(v2) < 1e-8:
        raise ValueError("In-plane vectors must be non-zero.")
    normal = np.cross(v1, v2)
    if np.linalg.norm(normal) < 1e-8:
        raise ValueError("In-plane vectors are collinear; they cannot span a plane.")

    # Mounted-sample (component) frame: x, z horizontal (scattering plane); y vertical.
    # Map the first in-plane crystal direction to x, the plane normal to y, and the
    # right-handed completion to z. {e1, en, e3} is orthonormal and right-handed, so
    # U = T_crystal^T is a proper rotation (det = +1).
    e1 = v1 / np.linalg.norm(v1)
    en = normal / np.linalg.norm(normal)
    e3 = np.cross(e1, en)
    T_crystal = np.column_stack([e1, en, e3])
    U = T_crystal.T
    ub.set_U(U)
    sample.orientation_set = True

    return scattering_plane_info(sample)


def scattering_plane_info(sample: SampleState) -> dict:
    """Report the current scattering plane: normal and in-plane vectors in hkl."""
    sample = _require_oriented(sample)
    info = get_scattering_plane_info(sample.ub().U, sample.ub().B)
    return {
        "plane_normal_hkl": [float(x) for x in info["plane_normal_hkl"]],
        "in_plane_vector1_hkl": [float(x) for x in info["in_plane_vector1_hkl"]],
        "in_plane_vector2_hkl": [float(x) for x in info["in_plane_vector2_hkl"]],
        "chi_misalignment_deg": info["chi_misalignment_deg"],
        "omega_offset_deg": info["omega_offset_deg"],
    }


def scattering_plane_check(sample: SampleState, h: float, k: float, l: float) -> dict:
    """Is reflection (hkl) in the horizontal scattering plane? Report tilt needed."""
    sample = _require_oriented(sample)
    qc = sample.ub().hkl_to_q(h, k, l)
    q_inst = component_q_to_instrument_q(np.asarray(qc, dtype=float))
    qmag = float(np.linalg.norm(q_inst))
    if qmag < 1e-9:
        raise ValueError("(0,0,0) has no direction in the plane.")
    # Instrument frame: z is vertical (out of plane). Out-of-plane angle from horizontal.
    out_of_plane_deg = math.degrees(math.asin(np.clip(q_inst[2] / qmag, -1.0, 1.0)))
    in_plane = abs(out_of_plane_deg) <= _IN_PLANE_TOL_DEG
    return {
        "hkl_rlu": [float(h), float(k), float(l)],
        "q_inv_angstrom": [float(x) for x in q_inst],
        "frame": "instrument",
        "in_plane": in_plane,
        "out_of_plane_angle_deg": out_of_plane_deg,
        "note": ("In plane." if in_plane else
                 "Out of plane: requires a sample tilt of about "
                 f"{abs(out_of_plane_deg):.2f} deg, or a remount."),
    }
