# Vendored from TAS_MCP (user's own repo, no LICENSE file; same author as ISAR),
# original path tas_mcp/feasibility.py.  Vendored 2026-07-11, adapted: (1) the tavi import shim
# replaced with the vendored `.reciprocal_space` / `.tas_geometry` primitives; (2) made
# stateless — `check_feasibility` / `accessible_range` take a `SampleState` plus explicit
# fixed-mode / fixed-k / instrument arguments (default = PUMA) instead of resolving a mutable
# `TASSession`; the `session.resolve(...)` transient-copy step is dropped.
"""Kinematic feasibility PREVIEW.

check_feasibility answers: for a target (Q, E) in a given fixed-k mode, does the
scattering triangle close, what are the instrument angles, do the monochromator/analyzer
have a Bragg reflection, is the point in the scattering plane, and does anything violate a
confirmed instrument limit?

This is a PREVIEW only. TAVI's HTTP /validate endpoint remains authoritative for the drive
loop; this layer exists so the GUI / planner can screen points cheaply without a round-trip.

Conventions:
    * Energy transfer ``e_transfer_meV`` > 0 means neutron ENERGY LOSS (ki > kf), i.e.
      an excitation is created in the sample (e.g. a phonon). E = 2.0717*(ki^2 - kf^2).
    * Sample two-theta (stt / A4) follows tavi's sign convention (returned negative).
    * |Q| and the triangle math need no orientation. The in-plane test needs a set
      orientation (U); without it, in_plane is null.

The kinematic checks are HARD (triangle closure, Bragg reflection existence). Mechanical
limits are SOFT in Phase 1: an unconfirmed (None) limit yields a warning, never an
infeasible verdict (see instruments.py).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .reciprocal_space import update_Q_from_HKL_direct
from .tas_geometry import stt_from_q_norm, solve_instrument_angles, component_q_to_instrument_q
from .instruments import InstrumentProfile, PUMA
from .sample_state import SampleState
from .units import HBAR2_OVER_2MN_MEV_A2

_IN_PLANE_TOL_DEG = 0.05


def _ki_kf_from_energy(e_transfer_meV: float, fixed_mode: str,
                       fixed_k: float) -> tuple[float, float, Optional[str]]:
    """Return (ki, kf, error) for the fixed-k mode. error is None if both are real."""
    de = e_transfer_meV / HBAR2_OVER_2MN_MEV_A2  # = ki^2 - kf^2
    if fixed_mode == "kf":
        kf = fixed_k
        ki_sq = kf * kf + de
        if ki_sq <= 0:
            return (0.0, kf, "energy_gain_exceeds_fixed_kf")
        return (math.sqrt(ki_sq), kf, None)
    elif fixed_mode == "ki":
        ki = fixed_k
        kf_sq = ki * ki - de
        if kf_sq <= 0:
            return (ki, 0.0, "energy_loss_exceeds_fixed_ki")
        return (ki, math.sqrt(kf_sq), None)
    raise ValueError("fixed_mode must be 'kf' or 'ki'.")


def _q_magnitude(sample: Optional[SampleState], hkl=None, q_inv_angstrom=None) -> float:
    if (hkl is None) == (q_inv_angstrom is None):
        raise ValueError("Provide exactly one of hkl or q_inv_angstrom.")
    if hkl is not None:
        if sample is None:
            raise ValueError("No sample set; cannot convert hkl to |Q|.")
        h, k, l = hkl
        return float(np.linalg.norm(update_Q_from_HKL_direct(h, k, l, *sample.lattice)))
    return float(np.linalg.norm(np.asarray(q_inv_angstrom, dtype=float)))


def check_feasibility(sample: Optional[SampleState], *, hkl=None, q_inv_angstrom=None,
                      e_transfer_meV: float = 0.0,
                      fixed_mode: Optional[str] = None,
                      fixed_k_inv_angstrom: Optional[float] = None,
                      instrument: InstrumentProfile = PUMA) -> dict:
    """Evaluate whether a single (Q, E) point is reachable.

    Provide the target as ``hkl`` (rlu, needs a sample) or ``q_inv_angstrom`` (a Q
    magnitude vector). Energy transfer in meV (loss positive). ``fixed_mode`` /
    ``fixed_k_inv_angstrom`` default to the instrument's operating defaults.
    """
    inst = instrument
    fixed_mode = fixed_mode if fixed_mode is not None else inst.default_fixed_mode
    fixed_k = (fixed_k_inv_angstrom if fixed_k_inv_angstrom is not None
               else inst.default_fixed_k_inv_angstrom)
    warnings: list[str] = []

    qmag = _q_magnitude(sample, hkl=hkl, q_inv_angstrom=q_inv_angstrom)

    result: dict = {
        "feasible": False,
        "reason": None,
        "e_transfer_meV": float(e_transfer_meV),
        "q_magnitude_inv_angstrom": qmag,
        "fixed_mode": fixed_mode,
        "frame": "instrument",
        "warnings": warnings,
    }
    if hkl is not None:
        result["hkl_rlu"] = [float(x) for x in hkl]

    # 1. ki, kf from the fixed-k mode.
    ki, kf, kerr = _ki_kf_from_energy(e_transfer_meV, fixed_mode, fixed_k)
    result["ki_inv_angstrom"] = ki
    result["kf_inv_angstrom"] = kf
    if kerr is not None:
        result["reason"] = kerr
        return result

    # 2. Triangle closure + sample two-theta (HARD).
    try:
        stt = stt_from_q_norm(qmag, ki, kf)
    except ValueError:
        result["reason"] = "scattering_triangle_does_not_close"
        result["triangle_closes"] = False
        result["q_min_inv_angstrom"] = abs(ki - kf)
        result["q_max_inv_angstrom"] = ki + kf
        return result
    result["triangle_closes"] = True
    result["stt_deg"] = stt

    # 3. Monochromator / analyzer Bragg reflections (HARD: must exist).
    try:
        mono_tt = inst.bragg_two_theta_deg(ki, "mono")
        ana_tt = inst.bragg_two_theta_deg(kf, "analyzer")
    except ValueError as exc:
        result["reason"] = "no_bragg_reflection_at_mono_or_analyzer"
        result["detail"] = str(exc)
        return result
    result["mono_two_theta_deg"] = mono_tt
    result["ana_two_theta_deg"] = ana_tt

    # 4. In-plane test + sample angles (needs orientation; otherwise null).
    if sample is not None and sample.oriented and hkl is not None:
        qc = sample.ub().hkl_to_q(*hkl)
        q_inst = component_q_to_instrument_q(np.asarray(qc, dtype=float))
        angles = solve_instrument_angles(q_inst, ki, kf)
        result["sth_deg"] = angles.sth
        result["saz_deg"] = angles.saz
        result["in_plane"] = abs(angles.saz) <= _IN_PLANE_TOL_DEG
        if not result["in_plane"]:
            warnings.append(
                f"Target is out of the horizontal plane (saz={angles.saz:.2f} deg); "
                "requires a sample tilt/goniometer or a remount."
            )
    else:
        result["in_plane"] = None
        warnings.append(
            "in_plane not determined: set a sample orientation (peaks/plane) or use "
            "scattering_plane_check."
        )

    # 5. Mechanical limits (SOFT in Phase 1).
    if inst.k_min_inv_angstrom is None or inst.k_max_inv_angstrom is None:
        warnings.append("k range unconfirmed for this instrument (soft check).")
    else:
        for label, kval in (("ki", ki), ("kf", kf)):
            if not (inst.k_min_inv_angstrom <= kval <= inst.k_max_inv_angstrom):
                result["reason"] = f"{label}_out_of_k_range"
                return result

    for limit, angle, name in (
        (inst.mono_two_theta_limit, mono_tt, "mono_two_theta"),
        (inst.ana_two_theta_limit, ana_tt, "ana_two_theta"),
        (inst.sample_two_theta_limit, stt, "sample_two_theta"),
    ):
        if not limit.confirmed:
            warnings.append(f"{name} limit unconfirmed (soft check).")
        elif not limit.contains(angle):
            result["reason"] = f"{name}_out_of_range"
            return result

    # All hard checks passed (and no confirmed limit was violated).
    result["feasible"] = True
    result["reason"] = "ok"
    return result


def accessible_range(sample: Optional[SampleState], *, hkl=None, q_inv_angstrom=None,
                     e_min_meV: float = 0.0, e_max_meV: float = 20.0,
                     e_step_meV: float = 1.0,
                     fixed_mode: Optional[str] = None,
                     fixed_k_inv_angstrom: Optional[float] = None,
                     instrument: InstrumentProfile = PUMA) -> dict:
    """Scan energy transfer at a fixed Q and report the reachable window.

    Walks e_min..e_max in steps and records, for each point, whether it is feasible and
    (if not) why. Returns the contiguous feasible band plus the per-point verdicts so the
    caller can see exactly where (and why) coverage runs out along, e.g., a dispersion.
    """
    if e_step_meV <= 0:
        raise ValueError("e_step_meV must be positive.")
    n = int(math.floor((e_max_meV - e_min_meV) / e_step_meV + 0.5)) + 1
    energies = [round(e_min_meV + i * e_step_meV, 6) for i in range(max(n, 1))]

    points = []
    feasible_energies = []
    for e in energies:
        verdict = check_feasibility(
            sample, hkl=hkl, q_inv_angstrom=q_inv_angstrom, e_transfer_meV=e,
            fixed_mode=fixed_mode, fixed_k_inv_angstrom=fixed_k_inv_angstrom,
            instrument=instrument,
        )
        points.append({
            "e_transfer_meV": e,
            "feasible": verdict["feasible"],
            "reason": verdict["reason"],
            "ki_inv_angstrom": verdict.get("ki_inv_angstrom"),
            "kf_inv_angstrom": verdict.get("kf_inv_angstrom"),
            "stt_deg": verdict.get("stt_deg"),
        })
        if verdict["feasible"]:
            feasible_energies.append(e)

    band = None
    if feasible_energies:
        band = {"e_min_meV": min(feasible_energies), "e_max_meV": max(feasible_energies)}

    return {
        "hkl_rlu": [float(x) for x in hkl] if hkl is not None else None,
        "q_inv_angstrom": list(q_inv_angstrom) if q_inv_angstrom is not None else None,
        "feasible_band_meV": band,
        "n_feasible": len(feasible_energies),
        "n_total": len(energies),
        "points": points,
    }
