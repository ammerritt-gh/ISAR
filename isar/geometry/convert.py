# Vendored from TAS_MCP (user's own repo, no LICENSE file; same author as ISAR),
# original path tas_mcp/convert.py.  Vendored 2026-07-11, adapted: (1) the tavi import shim
# (`from . import _tavi`; `from tavi...`) replaced with the vendored `.reciprocal_space` /
# `.tas_geometry` primitives; (2) made stateless — every function takes a `SampleState` per
# call instead of reading a mutable `TASSession.sample`; `bragg_two_theta` takes an explicit k
# (default = PUMA fixed kf) instead of `session.fixed_k_inv_angstrom`; (3) 2026-07-12
# `bragg_two_theta` tolerates float overshoot at the Ewald boundary (raise only when
# ratio > 1+1e-9, then clamp into [0,1]) so |Q|==2k gives two-theta=180 without raising.
"""HKL <-> Q conversions, d-spacing, and Bragg angle.

Thin wrappers over the vendored TAVI primitives. Orientation-independent magnitudes (|Q|,
d-spacing) use the lattice-only path in ``reciprocal_space``; oriented HKL->Q uses
``UBMatrix`` so the mounted-crystal Q matches TAVI exactly. Outputs carry unit suffixes and
an explicit ``frame`` label. Oriented Q is in the INSTRUMENT frame (z out-of-plane).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .reciprocal_space import update_Q_from_HKL_direct, update_HKL_from_Q_direct
from .tas_geometry import component_q_to_instrument_q, instrument_q_to_component_q
from .instruments import PUMA
from .sample_state import SampleState


def hkl_to_q(sample: SampleState, h: float, k: float, l: float) -> dict:
    """Convert Miller indices to Cartesian Q (A^-1) and |Q|.

    If the sample is oriented (U set), Q is in the instrument frame via the UB matrix.
    Otherwise Q is in the crystal frame (lattice only); |Q| is identical either way.
    """
    qmag = float(np.linalg.norm(
        update_Q_from_HKL_direct(h, k, l, *sample.lattice)
    ))
    out = {
        "hkl_rlu": [float(h), float(k), float(l)],
        "q_magnitude_inv_angstrom": qmag,
        "d_spacing_angstrom": (2.0 * math.pi / qmag) if qmag > 0 else math.inf,
    }
    if sample.oriented:
        qc = sample.ub().hkl_to_q(h, k, l)
        qx, qy, qz = component_q_to_instrument_q(np.asarray(qc, dtype=float))
        out["q_inv_angstrom"] = [float(qx), float(qy), float(qz)]
        out["frame"] = "instrument"
    else:
        qx, qy, qz = update_Q_from_HKL_direct(h, k, l, *sample.lattice)
        out["q_inv_angstrom"] = [float(qx), float(qy), float(qz)]
        out["frame"] = "crystal"
    return out


def q_to_hkl(sample: SampleState, qx: float, qy: float, qz: float) -> dict:
    """Convert Cartesian Q (A^-1) to Miller indices.

    Uses the UB matrix when oriented (interprets Q in the instrument frame); otherwise
    inverts the lattice-only reciprocal metric (interprets Q in the crystal frame).
    """
    if sample.oriented:
        qc = instrument_q_to_component_q(np.array([float(qx), float(qy), float(qz)]))
        h, k, l = sample.ub().q_to_hkl(*qc)
        frame = "instrument"
    else:
        h, k, l = update_HKL_from_Q_direct(qx, qy, qz, *sample.lattice)
        frame = "crystal"
    return {
        "q_inv_angstrom": [float(qx), float(qy), float(qz)],
        "frame": frame,
        "hkl_rlu": [float(h), float(k), float(l)],
    }


def d_spacing(sample: SampleState, h: float, k: float, l: float) -> dict:
    """d-spacing (A) and |Q| (A^-1) for an (hkl) reflection. Orientation-independent."""
    qmag = float(np.linalg.norm(update_Q_from_HKL_direct(h, k, l, *sample.lattice)))
    if qmag <= 0:
        raise ValueError("(0,0,0) has no d-spacing.")
    return {
        "hkl_rlu": [float(h), float(k), float(l)],
        "q_magnitude_inv_angstrom": qmag,
        "d_spacing_angstrom": 2.0 * math.pi / qmag,
    }


def bragg_two_theta(sample: SampleState, h: float, k: float, l: float,
                    k_inv_angstrom: Optional[float] = None) -> dict:
    """Sample scattering angle (2-theta, deg) for an elastic Bragg peak (hkl).

    Elastic condition |Q| = 2 k sin(theta), so 2-theta = 2 asin(|Q| / (2k)).

    Args:
        k_inv_angstrom: Wavevector to use. Defaults to the PUMA fixed-k value
            (for elastic alignment ki = kf = k).
    """
    kk = k_inv_angstrom if k_inv_angstrom is not None else PUMA.default_fixed_k_inv_angstrom
    if kk <= 0:
        raise ValueError("k must be positive.")
    qmag = float(np.linalg.norm(update_Q_from_HKL_direct(h, k, l, *sample.lattice)))
    ratio = qmag / (2.0 * kk)
    # A tiny floating-point overshoot at the Ewald-sphere boundary (|Q| == 2k, i.e.
    # two-theta == 180) must not raise; only a genuinely unreachable reflection does.
    if ratio > 1.0 + 1e-9:
        raise ValueError(
            f"(hkl) unreachable elastically at k={kk:.4f} A^-1: |Q|={qmag:.4f} > 2k="
            f"{2 * kk:.4f}. The Bragg peak is outside the Ewald sphere."
        )
    ratio = min(1.0, max(0.0, ratio))
    two_theta = 2.0 * math.degrees(math.asin(ratio))
    return {
        "hkl_rlu": [float(h), float(k), float(l)],
        "k_inv_angstrom": kk,
        "q_magnitude_inv_angstrom": qmag,
        "sample_two_theta_deg": two_theta,
    }
