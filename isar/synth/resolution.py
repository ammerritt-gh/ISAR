"""TAS energy resolution by the Cooper-Nathans method (docs/archive/PHASE_3_DESIGN.md §8, D5).

The marginal (vanadium) energy-resolution FWHM at the elastic line, computed from a numor
header (DM/DA/KFIX/FX/ALF*/BET*/ETA*) — the right width to convolve a constant-Q S(E)
against on a triple-axis instrument where no vanadium scan exists to measure it. Ported to
Python from the ResCal ``rc_cnmat`` formulation (Cooper & Nathans, Acta Cryst. 23, 357
(1967)); only the resolution matrix is used, not the intensity prefactor.

This is the scalar precursor to the full 4-D tilted-ellipsoid engine (``tas4d``, D5): it
gives a per-scan Gaussian FWHM (the elastic energy width) but not yet the dispersion-slope
focusing that makes intrinsic Γ trustworthy on TAS. Promoted verbatim from the Phase-1
check (``docs/in8_phase1_check.py``) so the fit and the check share one implementation.
"""
from __future__ import annotations

import math

import numpy as np

FALLBACK_FWHM = 0.6              # meV — used when the scattering triangle cannot be closed

_PIT = math.pi / (180 * 60)                  # arcmin -> rad
_F = 1.0 / 2.072142                          # meV -> Angstrom^-2  (E = 2.072142 k^2)
_S2L = 2.0 * math.sqrt(2.0 * math.log(2.0))  # sigma -> FWHM


def _marginalize(M, i):
    keep = [j for j in range(M.shape[0]) if j != i]
    return M[np.ix_(keep, keep)] - np.outer(M[keep, i], M[i, keep]) / M[i, i]


def cn_resolution_matrix(p, q0, w):
    """Cooper-Nathans 4x4 resolution matrix in (dQx,dQy,dQz,dE), FWHM-normalized.

    ``p`` is the numor header ``params`` dict, ``q0`` the |Q| (Angstrom^-1), ``w`` the energy
    transfer (meV). Returns ``None`` if the scattering triangle cannot be closed for this scan.
    """
    dm, da = p["DM"], p["DA"]
    etam, etaa = p["ETAM"] * _PIT, p["ETAA"] * _PIT
    etas = p.get("ETAS", p["ETAM"]) * _PIT
    sm, ss, sa = p["SM"], p["SS"], p["SA"]
    kfix, fx = p["KFIX"], int(p["FX"])
    alf0, alf1, alf2, alf3 = (p[k] * _PIT for k in ("ALF1", "ALF2", "ALF3", "ALF4"))
    bet0, bet1, bet2, bet3 = (p[k] * _PIT for k in ("BET1", "BET2", "BET3", "BET4"))

    ki = math.sqrt(kfix**2 + (fx - 1) * _F * w)
    kf = math.sqrt(kfix**2 - (2 - fx) * _F * w)
    c2t = (ki**2 + kf**2 - q0**2) / (2 * ki * kf)
    if abs(c2t) > 1:
        return None
    thetam, thetaa = math.asin(math.pi / (dm * ki)), math.asin(math.pi / (da * kf))
    tan, sin = math.tan, math.sin

    N = np.zeros((6, 6))
    pm = 1 / (ki * etam) * np.array([sm * tan(thetam), 1.0])
    palf0 = 1 / (ki * alf0) * np.array([2 * sm * tan(thetam), 1.0])
    palf1 = 1 / (ki * alf1) * np.array([0.0, 1.0])
    pa = 1 / (kf * etaa) * np.array([-sa * tan(thetaa), 1.0])
    palf3 = 1 / (kf * alf3) * np.array([-2 * sa * tan(thetaa), 1.0])
    palf2 = 1 / (kf * alf2) * np.array([0.0, 1.0])
    N[0:2, 0:2] = np.outer(pm, pm) + np.outer(palf0, palf0) + np.outer(palf1, palf1)
    N[3:5, 3:5] = np.outer(pa, pa) + np.outer(palf3, palf3) + np.outer(palf2, palf2)
    N[2, 2] = (1 / bet1**2 + 1 / ((2 * sin(thetam) * etam)**2 + bet0**2)) / ki**2
    N[5, 5] = (1 / bet2**2 + 1 / ((2 * sin(thetaa) * etaa)**2 + bet3**2)) / kf**2

    ang1 = math.acos(-(kf**2 - q0**2 - ki**2) / (2 * q0 * ki))
    ang2 = math.pi - math.acos(-(ki**2 - q0**2 - kf**2) / (2 * q0 * kf))
    TI = np.array([[math.cos(ang1), -ss * math.sin(ang1)], [ss * math.sin(ang1), math.cos(ang1)]])
    TF = np.array([[math.cos(ang2), -ss * math.sin(ang2)], [ss * math.sin(ang2), math.cos(ang2)]])
    B = np.zeros((6, 6))
    B[0:2, 0:2] = TI; B[0:2, 3:5] = -TF; B[2, 2] = 1; B[2, 5] = -1
    B[3, 0] = 2 * ki / _F; B[3, 3] = -2 * kf / _F; B[4, 0] = 1; B[5, 2] = 1

    V = np.linalg.inv(B)
    Nold = V.T @ N @ V
    Nold = _marginalize(Nold, 5)            # integrate out kiz
    Nold = _marginalize(Nold, 4)            # integrate out kix -> 4x4 (dQx,dQy,dQz,dE)
    NP = Nold - np.outer(Nold[:, 1], Nold[:, 1]) / (1 / (etas * q0)**2 + Nold[1, 1])
    NP[2, 2] = Nold[2, 2]
    return 8 * math.log(2) * NP


def qmod(p):
    """|Q| in Angstrom^-1 from (QH,QK,QL) and the orthorhombic cell (AS,BS,CS); angles 90 deg."""
    return 2 * math.pi * math.sqrt((p["QH"] / p["AS"])**2 + (p["QK"] / p["BS"])**2
                                   + (p["QL"] / p["CS"])**2)


def cn_energy_fwhm(rec, w=0.0, fallback=FALLBACK_FWHM):
    """Marginal (vanadium) energy-resolution FWHM (meV) for a scan, by Cooper-Nathans at transfer ``w``.

    Reads the numor header from ``rec.raw_header["params"]``. Returns ``fallback`` if the CN
    triangle cannot be closed or the header lacks the geometry keys.
    """
    p = rec.raw_header["params"]
    try:
        NP = cn_resolution_matrix(p, qmod(p), w)
        if NP is None:
            return fallback
        return float(_S2L * math.sqrt(np.linalg.inv(NP)[3, 3]))
    except (KeyError, ValueError, ZeroDivisionError, np.linalg.LinAlgError):
        return fallback
