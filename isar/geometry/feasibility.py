# Vendored from TAS_MCP (user's own repo, no LICENSE file; same author as ISAR),
# original path tas_mcp/feasibility.py.  Vendored 2026-07-11, adapted: (1) the tavi import shim
# replaced with the vendored `.reciprocal_space` / `.tas_geometry` primitives; (2) made
# stateless — `check_feasibility` / `accessible_range` take a `SampleState` plus explicit
# fixed-mode / fixed-k / instrument arguments (default = PUMA) instead of resolving a mutable
# `TASSession`; the `session.resolve(...)` transient-copy step is dropped; (3) 2026-07-12
# `accessible_range` hardened — energy grid never overshoots e_max (and e_max<e_min raises),
# scalar `q_inv_angstrom` serializes safely, and feasibility is reported as contiguous runs
# (`feasible_bands_meV`; `feasible_band_meV` set only for a single run) instead of a min/max
# span that merged disjoint runs across an infeasible gap; (4) 2026-10-01 the sample angles
# are TAVI 010cec6d's per-point stage solve (instruments/tas_runtime.py,
# `calculate_stage_angles`) on the instrument's goniometer; see isar/geometry/VENDORED.md.
"""Kinematic feasibility PREVIEW.

check_feasibility answers: for a target (Q, E) in a given fixed-k mode, does the
scattering triangle close, what are the instrument angles, can the sample stage bring Q
into the scattering plane, do the monochromator/analyzer have a Bragg reflection, is the
point in the scattering plane, and does anything violate a confirmed instrument limit?

This is a PREVIEW only. TAVI's HTTP /validate endpoint remains authoritative for the drive
loop; this layer exists so the GUI / planner can screen points cheaply without a round-trip.

Conventions:
    * Energy transfer ``e_transfer_meV`` > 0 means neutron ENERGY LOSS (ki > kf), i.e.
      an excitation is created in the sample (e.g. a phonon). E = 2.0717*(ki^2 - kf^2).
    * Sample two-theta (stt / A4) carries the instrument's sample sense (PUMA: -1, negative).
    * |Q| and the triangle math need no orientation. The in-plane test and the stage
      angles need a set orientation (U); without it, in_plane is null.
    * The stage angles are TAVI's readouts: ``sth_deg`` the turntable A3, ``sgl_deg`` and
      ``sgu_deg`` the lower and upper arcs, the smallest tilt that levels Q.

The kinematic checks are HARD (triangle closure, Bragg reflection existence), and so is the
stage's reach (``stage_out_of_travel``, TAVI's refusal text in ``detail``), as TAVI's
/validate refuses it. The other mechanical limits are SOFT in Phase 1: an unconfirmed
(None) limit yields a warning, never an infeasible verdict (see instruments.py).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .reciprocal_space import update_Q_from_HKL_direct
from .tas_geometry import (
    _normalize_deg,
    component_q_to_instrument_q,
    lab_q_from_stt,
    stt_from_q_norm,
)
from .orientation import StageUnreachable, solve_stage
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
        stt = stt_from_q_norm(qmag, ki, kf, inst.sense_sample)
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

    # 4. In-plane test + the sample stage's angles (needs orientation; otherwise null):
    #    TAVI 010cec6d's per-point stage solve (instruments/tas_runtime.py 920-934).
    if sample is not None and sample.oriented and hkl is not None:
        q_mount = np.asarray(sample.ub().hkl_to_q(*hkl), dtype=float)
        q_inst = component_q_to_instrument_q(q_mount)
        elevation = math.degrees(math.atan2(q_inst[2], math.hypot(q_inst[0], q_inst[1])))
        result["in_plane"] = abs(elevation) <= _IN_PLANE_TOL_DEG
        # vTAS Friedel convention: the +1 branch aligns -U B hkl with Q_lab.
        signed = -q_mount if inst.sense_sample > 0 else q_mount
        try:
            stage = solve_stage(inst.goniometer, signed, lab_q_from_stt(ki, kf, stt))
        except StageUnreachable as exc:
            # Every refusal of a two-arc stage is a travel limit (or no arc setting at all).
            result["reason"] = "stage_out_of_travel"
            result["detail"] = str(exc)
            return result
        sth = stage["A3"]
        if inst.sense_sample > 0:
            sth = _normalize_deg(sth)   # the legacy +1-branch readout
        result["sth_deg"] = sth
        result["sgl_deg"] = stage["sgl"]
        result["sgu_deg"] = stage["sgu"]
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
    (if not) why. ``feasible_band_meV`` is the single contiguous feasible band when there is
    exactly one, else None; ``feasible_bands_meV`` always lists every contiguous feasible run
    (empty when nothing is feasible), so a dispersion that dips out of range and back is not
    silently merged across the gap. The per-point verdicts show exactly where (and why)
    coverage runs out.
    """
    if e_step_meV <= 0:
        raise ValueError("e_step_meV must be positive.")
    if e_max_meV < e_min_meV:
        raise ValueError("e_max_meV must be >= e_min_meV.")
    # Generate e_min, e_min+step, ... up to (and including) e_max; never overshoot e_max.
    energies = []
    i = 0
    while True:
        e = e_min_meV + i * e_step_meV
        if e > e_max_meV + 1e-9:
            break
        energies.append(round(e, 6))
        i += 1

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

    # Contiguous feasible runs over the energy grid: a feasible-infeasible-feasible
    # pattern yields two runs and must NOT be merged into one band across the gap.
    runs = []
    current = None
    for pt in points:
        if pt["feasible"]:
            if current is None:
                current = {"e_min_meV": pt["e_transfer_meV"], "e_max_meV": pt["e_transfer_meV"]}
            else:
                current["e_max_meV"] = pt["e_transfer_meV"]
        elif current is not None:
            runs.append(current)
            current = None
    if current is not None:
        runs.append(current)

    # The single-band contract: set only when there is exactly one contiguous run.
    band = runs[0] if len(runs) == 1 else None

    return {
        "hkl_rlu": [float(x) for x in hkl] if hkl is not None else None,
        "q_inv_angstrom": ([float(v) for v in np.atleast_1d(np.asarray(q_inv_angstrom, dtype=float))]
                           if q_inv_angstrom is not None else None),
        "feasible_band_meV": band,
        "feasible_bands_meV": runs,
        "n_feasible": len(feasible_energies),
        "n_total": len(energies),
        "points": points,
    }
