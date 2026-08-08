"""Test dispersions for the (2,0,0) → (3,0,0) Q-series.

ω₀(H) = 6·|sin(π(H−2))| meV: zero at the zone centers (H = 2, 3), maximum 6 meV at
the zone boundary (H = 2.5). Walking H sweeps the fit from easy (mid-zone, ω₀ ≫ Γ,
clear of the elastic line) to hard (near zone center, ω₀ → 0 → Stokes–elastic merge
and the overdamped boundary).

`BranchSpec` generalizes that single acoustic curve to any number of independent
branches on the same path — gapped, dispersionless, descending — so a SCENE world can
plant two branches that cross. The sine shape is kept for all of them: it is the one
zone-periodic curve the harness has ever used, and reusing it means a crossing world
differs from the canonical world only in its zone-centre/zone-boundary energies.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

# (2,0,0) → (3,0,0): H from 2.00 to 3.00, step 0.05.
H_START = 2.0
H_STOP = 3.0
H_STEP = 0.05

# Dispersion amplitude (meV): maximum ω₀ at the zone boundary.
OMEGA0_MAX = 6.0


def omega0_of_H(H):
    """Undamped phonon frequency ω₀(H) = 6·|sin(π(H−2))| meV. Scalar or array."""
    H = np.asarray(H, dtype=float)
    out = OMEGA0_MAX * np.abs(np.sin(np.pi * (H - H_START)))
    return out if out.ndim else float(out)


def h_values():
    """The 21-point H grid, 2.00 → 3.00 inclusive at step 0.05 (rounded to 2 dp)."""
    n = int(round((H_STOP - H_START) / H_STEP)) + 1
    return np.round(H_START + H_STEP * np.arange(n), 2)


# ----------------------------------------------------------------------------
# General branches (SCENE worlds)
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class BranchSpec:
    """One excitation branch: ω₀(H) = e_zc + (e_zb − e_zc)·|sin(π(H−H_start))|.

    ``e_zc`` is the zone-centre energy (0 for an acoustic branch, finite for a gapped or
    optic one) and ``e_zb`` the zone-boundary energy. ``e_zb < e_zc`` gives a descending
    branch; ``e_zb == e_zc`` a dispersionless level. ``name`` is a ground-truth label
    only — nothing in the generator infers an identity from it.
    """

    name: str
    e_zc: float
    e_zb: float
    H_start: float = H_START

    def omega0(self, H):
        """ω₀ at H (scalar or array), in meV."""
        H = np.asarray(H, dtype=float)
        out = self.e_zc + (self.e_zb - self.e_zc) * np.abs(np.sin(np.pi * (H - self.H_start)))
        return out if out.ndim else float(out)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BranchSpec":
        # Same fail-loudly contract as config._reject_unknown_keys (inlined: config
        # imports dispersion, so dispersion must not import config).
        unexpected = set(d) - {"name", "e_zc", "e_zb", "H_start"}
        if unexpected:
            raise ValueError(f"BranchSpec.from_dict: unexpected key(s) {sorted(unexpected)!r}")
        return cls(name=str(d["name"]), e_zc=float(d["e_zc"]), e_zb=float(d["e_zb"]),
                   H_start=float(d.get("H_start", H_START)))


ACOUSTIC = BranchSpec("acoustic", 0.0, OMEGA0_MAX)


def crossing_H(a: BranchSpec, b: BranchSpec) -> float | None:
    """H in the first half-zone where branches ``a`` and ``b`` are degenerate, else None.

    Both branches are affine in s = |sin(π(H−H_start))|, so the crossing is the s where
    the two lines meet; it is real only if that s lies in [0, 1]. Returns the ascending
    solution H = H_start + arcsin(s)/π (the mirror solution in the second half-zone is
    implied by the periodicity).
    """
    if a.H_start != b.H_start:
        raise ValueError("branches must share H_start to be compared on one path")
    slope = (a.e_zb - a.e_zc) - (b.e_zb - b.e_zc)
    if slope == 0:
        return None                      # parallel: never cross (or coincide everywhere)
    s = (b.e_zc - a.e_zc) / slope
    if not 0.0 <= s <= 1.0:
        return None
    return a.H_start + math.asin(s) / math.pi


def classify_cell(H, gamma, eps_omega_d):
    """Label a cell by its position on the dispersion.

    - ``unmeasurable``: ω₀ < ε (zone-center/Bragg; phonon buried in the elastic line).
    - ``boundary``: ε ≤ ω₀ ≲ 2Γ (Stokes within ~2 linewidths of the elastic line).
    - ``midzone``: ω₀ ≳ 2Γ (the happy path).
    """
    omega0 = omega0_of_H(H)
    if omega0 < eps_omega_d:
        return "unmeasurable"
    if omega0 < 2.0 * gamma:
        return "boundary"
    return "midzone"
