# Vendored from TAS_MCP (user's own repo, no LICENSE file; same author as ISAR),
# original path tas_mcp/instruments/puma.py.  Vendored 2026-07-11, adapted: flattened from the
# tas_mcp/instruments/ package into a single module (stdlib only; no import changes);
# 2026-07-12 `bragg_two_theta_deg` rejects any `which` other than "mono"/"analyzer"
# (previously anything non-"mono" silently selected the analyzer). 2026-10-01: TAVI's
# `tas_goniometer` (TAVI 010cec6d, instruments/descriptor.py) added over the vendored
# `StageAxis`, and `InstrumentProfile` gains TAVI's `sense_sample` and `goniometer`
# (PUMA: -1, +/-20 deg arcs); see isar/geometry/VENDORED.md.
"""PUMA instrument profile.

Phase 1 hardcodes a single PUMA profile. Operating defaults (crystals, default kf,
collimations, fixed mode) are taken verbatim from TAVI/config/parameters.json and are
trusted. Hard MECHANICAL LIMITS (k range, A2/A4 ranges, max field) are NOT present in
parameters.json; per the design decision they are deferred to a future limits config.
Until confirmed they are left as ``None`` and treated as SOFT checks by the feasibility
layer (a None limit produces a warning, never a hard infeasible verdict).

The profile is a plain dataclass so it can be serialized to / loaded from JSON when the
multi-instrument limits config arrives, with no change to call sites.

KNOWN GAP (do not "fix"): dark angles / mechanical limits have no numeric data anywhere,
so every ``AngleLimit`` here stays None (soft).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .orientation import StageAxis

# Travel no source documents: enforced as unlimited (TAVI's
# instruments.descriptor.UNDOCUMENTED).
UNDOCUMENTED = math.inf


def tas_goniometer(arc_travel: float = UNDOCUMENTED) -> tuple[StageAxis, ...]:
    """A TAS sample stage as TAVI's ``instruments.descriptor.tas_goniometer``
    declares it: turntable ``A3`` about y carrying a lower arc ``sgl`` about x
    and an upper arc ``sgu`` about z, each arc within +/-``arc_travel``
    degrees. The turntable turns freely. Carried over ``StageAxis``: TAVI's
    operator corrections and hidden zero errors have no use in ISAR."""
    return (
        StageAxis("A3", (0.0, 1.0, 0.0)),
        StageAxis("sgl", (1.0, 0.0, 0.0), -arc_travel, arc_travel),
        StageAxis("sgu", (0.0, 0.0, 1.0), -arc_travel, arc_travel),
    )


# d-spacing of pyrolytic graphite (002), angstrom. Cross-checked: with k=2.6634,
# sin(theta) = pi/(d*k) = 0.3516 -> 2*theta = 41.17 deg, matching mtt_var in
# parameters.json (41.167). See tests/geometry/test_units.py.
D_PG002_ANGSTROM = 3.3554


@dataclass(frozen=True)
class CrystalReflection:
    """A monochromator/analyzer crystal reflection."""

    name: str
    d_angstrom: float


@dataclass(frozen=True)
class AngleLimit:
    """A closed angular range in degrees. None bounds mean 'unconfirmed' (soft)."""

    min_deg: Optional[float] = None
    max_deg: Optional[float] = None

    @property
    def confirmed(self) -> bool:
        return self.min_deg is not None and self.max_deg is not None

    def contains(self, angle_deg: float) -> bool:
        """True if angle is inside the range. Unconfirmed bounds do not constrain."""
        lo_ok = self.min_deg is None or angle_deg >= self.min_deg
        hi_ok = self.max_deg is None or angle_deg <= self.max_deg
        return lo_ok and hi_ok


@dataclass(frozen=True)
class InstrumentProfile:
    """Static description of a TAS instrument for feasibility checks."""

    name: str
    monochromator: CrystalReflection
    analyzer: CrystalReflection

    # Operating defaults (trusted, from parameters.json).
    default_fixed_mode: str = "kf"          # "kf" or "ki"
    default_fixed_k_inv_angstrom: float = 2.6634
    default_collimations_arcmin: tuple = (60.0, 60.0, 60.0, 60.0)

    # Mechanical limits (DEFERRED config; None = unconfirmed -> soft check).
    k_min_inv_angstrom: Optional[float] = None
    k_max_inv_angstrom: Optional[float] = None
    mono_two_theta_limit: AngleLimit = field(default_factory=AngleLimit)
    ana_two_theta_limit: AngleLimit = field(default_factory=AngleLimit)
    sample_two_theta_limit: AngleLimit = field(default_factory=AngleLimit)
    max_field_tesla: Optional[float] = None

    # The sample stage, as TAVI's descriptor declares it: the sample scattering sense
    # (+1/-1, the sign of the sample two-theta readout) and the goniometer that reaches Q.
    sense_sample: int = -1
    goniometer: tuple = field(default_factory=tas_goniometer)

    def bragg_two_theta_deg(self, k_inv_angstrom: float, which: str) -> float:
        """Bragg 2-theta (deg) at a crystal for a beam of wavevector k.

        Uses lambda = 2 d sin(theta) with k = 2*pi/lambda, i.e. sin(theta) = pi/(d*k).

        Args:
            k_inv_angstrom: Beam wavevector magnitude (A^-1).
            which: "mono" or "analyzer".
        """
        if k_inv_angstrom <= 0:
            raise ValueError("k must be positive.")
        if which == "mono":
            crystal = self.monochromator
        elif which == "analyzer":
            crystal = self.analyzer
        else:
            raise ValueError(f"which must be 'mono' or 'analyzer'; got {which!r}.")
        sin_theta = math.pi / (crystal.d_angstrom * k_inv_angstrom)
        if sin_theta > 1.0:
            raise ValueError(
                f"k={k_inv_angstrom:.4f} A^-1 is too small for {crystal.name} "
                f"(d={crystal.d_angstrom} A): no Bragg reflection (sin(theta)>1)."
            )
        return 2.0 * math.degrees(math.asin(sin_theta))


PUMA = InstrumentProfile(
    name="PUMA",
    monochromator=CrystalReflection("PG002", D_PG002_ANGSTROM),
    analyzer=CrystalReflection("PG002", D_PG002_ANGSTROM),
    default_fixed_mode="kf",
    default_fixed_k_inv_angstrom=2.6634,
    default_collimations_arcmin=(60.0, 60.0, 60.0, 60.0),
    # All mechanical limits intentionally unconfirmed in Phase 1 (soft).
    # TAVI 010cec6d instruments/puma/plugin.py: Sense.RIGHT and tas_goniometer(20.0),
    # +/-20 deg arcs (the MLZ page's standard goniometer; TAVI's choice, not confirmed).
    sense_sample=-1,
    goniometer=tas_goniometer(20.0),
)
