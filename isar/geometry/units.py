# Vendored from TAS_MCP (user's own repo, no LICENSE file; same author as ISAR),
# original path tas_mcp/units.py.  Vendored 2026-07-11, adapted: 2026-07-12 the direct
# "energy_meV" input branch of `_to_energy_meV` rejects negative energies (matching the
# k/wavelength/velocity branches) instead of passing them through.
"""Neutron kinematic unit conversions and physical constants.

This is new (non-TAVI) code, but deliberately tiny and pinned to values that are
self-consistent with ``TAVI/config/parameters.json`` (PG[002], mtt=41.167 deg,
Ki=2.6634 / Ei=14.7). The self-consistency is enforced by ``tests/geometry/test_units.py``.

CRITICAL DISTINCTION (the whole reason this module is careful):
    * A WAVEVECTOR k (incident ki or final kf) maps to a neutron ENERGY.
    * A MOMENTUM TRANSFER Q (the abscissa of S(Q, E)) does NOT map to an energy.
      Q and E are independent axes. There is intentionally no Q->energy function here.

All energies are meV, all wavevectors are inverse angstrom (A^-1), all wavelengths
are angstrom (A), velocities m/s, temperatures K, frequencies THz, wavenumbers cm^-1.
"""
from __future__ import annotations

import math

# --- Primary constant -------------------------------------------------------
# hbar^2 / (2 m_n), in meV * A^2.  E[meV] = HBAR2_OVER_2MN * k[A^-1]^2.
# Value 2.072124 is the CODATA/McStas-consistent figure; it reproduces the
# textbook E = 81.8042 / lambda^2 and v = 437.39 * sqrt(E) exactly, and gives
# Ei = 14.699 meV for Ki = 2.6634 (rounds to the 14.7 in parameters.json).
HBAR2_OVER_2MN_MEV_A2 = 2.072124

# --- Derived / convenience constants (all exact given the above + 2*pi) -----
# E[meV] = E_LAMBDA2_MEV_A2 / lambda[A]^2, since E = HBAR2_OVER_2MN * (2*pi/lambda)^2.
E_LAMBDA2_MEV_A2 = HBAR2_OVER_2MN_MEV_A2 * (2.0 * math.pi) ** 2  # ~= 81.8042

# v[m/s] = NEUTRON_SPEED_COEFF * sqrt(E[meV]).  Derived from E = 1/2 m_n v^2.
# Pinned numeric value (CODATA m_n); checked against E<->v round trip in tests.
NEUTRON_SPEED_COEFF_M_PER_S = 437.3936  # m * s^-1 * meV^-1/2

# Energy-unit equivalences.
MEV_PER_THZ = 4.135667          # E[meV] = MEV_PER_THZ * nu[THz]   (Planck constant)
CM1_PER_MEV = 8.065544          # wavenumber[cm^-1] = CM1_PER_MEV * E[meV]
K_PER_MEV = 11.604518           # T[K] = K_PER_MEV * E[meV]

TWO_PI = 2.0 * math.pi

# Canonical quantities this module understands.
_QUANTITIES = ("energy_meV", "k_inv_angstrom", "wavelength_angstrom",
               "velocity_m_per_s", "frequency_THz", "wavenumber_per_cm",
               "temperature_K")


def energy_from_k(k_inv_angstrom: float) -> float:
    """Neutron energy (meV) from wavevector magnitude k (A^-1)."""
    if k_inv_angstrom < 0:
        raise ValueError("k must be non-negative.")
    return HBAR2_OVER_2MN_MEV_A2 * k_inv_angstrom * k_inv_angstrom


def k_from_energy(energy_meV: float) -> float:
    """Wavevector magnitude k (A^-1) from neutron energy (meV)."""
    if energy_meV < 0:
        raise ValueError("Neutron energy must be non-negative.")
    return math.sqrt(energy_meV / HBAR2_OVER_2MN_MEV_A2)


def wavelength_from_k(k_inv_angstrom: float) -> float:
    """Wavelength (A) from wavevector magnitude k (A^-1)."""
    if k_inv_angstrom <= 0:
        raise ValueError("k must be positive to define a wavelength.")
    return TWO_PI / k_inv_angstrom


def k_from_wavelength(wavelength_angstrom: float) -> float:
    """Wavevector magnitude k (A^-1) from wavelength (A)."""
    if wavelength_angstrom <= 0:
        raise ValueError("Wavelength must be positive.")
    return TWO_PI / wavelength_angstrom


def energy_from_wavelength(wavelength_angstrom: float) -> float:
    """Neutron energy (meV) from wavelength (A)."""
    if wavelength_angstrom <= 0:
        raise ValueError("Wavelength must be positive.")
    return E_LAMBDA2_MEV_A2 / (wavelength_angstrom * wavelength_angstrom)


def _to_energy_meV(value: float, quantity: str) -> float:
    """Convert any supported quantity to energy in meV (internal hub)."""
    if quantity == "energy_meV":
        if value < 0:
            raise ValueError("Neutron energy must be non-negative.")
        return float(value)
    if quantity == "k_inv_angstrom":
        return energy_from_k(value)
    if quantity == "wavelength_angstrom":
        return energy_from_wavelength(value)
    if quantity == "velocity_m_per_s":
        if value < 0:
            raise ValueError("Velocity must be non-negative.")
        return (value / NEUTRON_SPEED_COEFF_M_PER_S) ** 2
    if quantity == "frequency_THz":
        return float(value) * MEV_PER_THZ
    if quantity == "wavenumber_per_cm":
        return float(value) / CM1_PER_MEV
    if quantity == "temperature_K":
        return float(value) / K_PER_MEV
    raise ValueError(f"Unknown quantity {quantity!r}; expected one of {_QUANTITIES}.")


def _from_energy_meV(energy_meV: float, quantity: str) -> float:
    """Convert energy in meV to any supported quantity (internal hub)."""
    if quantity == "energy_meV":
        return energy_meV
    if quantity == "k_inv_angstrom":
        return k_from_energy(energy_meV)
    if quantity == "wavelength_angstrom":
        return wavelength_from_k(k_from_energy(energy_meV))
    if quantity == "velocity_m_per_s":
        return NEUTRON_SPEED_COEFF_M_PER_S * math.sqrt(energy_meV)
    if quantity == "frequency_THz":
        return energy_meV / MEV_PER_THZ
    if quantity == "wavenumber_per_cm":
        return energy_meV * CM1_PER_MEV
    if quantity == "temperature_K":
        return energy_meV * K_PER_MEV
    raise ValueError(f"Unknown quantity {quantity!r}; expected one of {_QUANTITIES}.")


def neutron_units(value: float, frm: str, to: str) -> dict:
    """Convert a single neutron quantity between energy/wavevector/wavelength/etc.

    Args:
        value: The numeric value to convert.
        frm: Source quantity, one of:
            energy_meV, k_inv_angstrom, wavelength_angstrom, velocity_m_per_s,
            frequency_THz, wavenumber_per_cm, temperature_K.
        to: Target quantity, same allowed set.

    Returns:
        dict with the input, the converted value under a unit-suffixed key, and
        (for transparency) the intermediate energy in meV.

    Notes:
        These conversions are ONLY valid for neutron wavevectors/energies. Do not
        pass a momentum transfer |Q| here expecting an energy; Q is not a wavevector
        in this sense and the result would be physically meaningless.
    """
    if frm not in _QUANTITIES:
        raise ValueError(f"Unknown source quantity {frm!r}; expected one of {_QUANTITIES}.")
    if to not in _QUANTITIES:
        raise ValueError(f"Unknown target quantity {to!r}; expected one of {_QUANTITIES}.")
    energy_meV = _to_energy_meV(value, frm)
    result = _from_energy_meV(energy_meV, to)
    return {
        "input": float(value),
        "from": frm,
        "to": to,
        to: result,
        "energy_meV": energy_meV,
    }
