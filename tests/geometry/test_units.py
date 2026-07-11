"""Constant self-consistency and unit round-trips (ported from TAS_MCP tests/test_units.py).

These pin the constants in ``isar.geometry.units`` to values consistent with
TAVI/config/parameters.json (PG[002], mtt=41.167, Ki=2.6634, Ei=14.7). Adapted only in the
import paths (tas_mcp -> isar.geometry).
"""
import pytest

from isar.geometry import units
from isar.geometry.instruments import PUMA


def test_energy_from_k_matches_parameters_json():
    # Ki=2.6634 should give Ei=14.7 meV (to parameters.json precision).
    assert units.energy_from_k(2.6634) == pytest.approx(14.7, abs=5e-3)


def test_k_from_energy_inverse():
    assert units.k_from_energy(14.7) == pytest.approx(2.6634, abs=5e-4)


def test_mono_two_theta_matches_mtt_var():
    # With PG002 and k=2.6634, mono 2-theta should match mtt_var=41.167 deg.
    tt = PUMA.bragg_two_theta_deg(2.6634, "mono")
    assert tt == pytest.approx(41.167, abs=0.02)


def test_e_lambda2_constant():
    # E = 81.8042 / lambda^2; cross-check via k.
    lam = units.wavelength_from_k(2.6634)
    assert units.energy_from_wavelength(lam) == pytest.approx(units.energy_from_k(2.6634))
    assert units.E_LAMBDA2_MEV_A2 == pytest.approx(81.8042, abs=1e-3)


@pytest.mark.parametrize("q", ["energy_meV", "k_inv_angstrom", "wavelength_angstrom",
                               "velocity_m_per_s", "frequency_THz",
                               "wavenumber_per_cm", "temperature_K"])
def test_roundtrip_through_energy(q):
    # Start from a fixed energy, convert out to q and back; should recover the energy.
    e0 = 14.7
    out = units.neutron_units(e0, "energy_meV", q)
    back = units.neutron_units(out[q], q, "energy_meV")
    assert back["energy_meV"] == pytest.approx(e0, rel=1e-9)


def test_velocity_value():
    # 14.7 meV neutron is ~1677 m/s (standard reference number).
    out = units.neutron_units(14.7, "energy_meV", "velocity_m_per_s")
    assert out["velocity_m_per_s"] == pytest.approx(1677.0, abs=2.0)


def test_q_is_not_a_wavevector_guard():
    # neutron_units rejects unknown quantities; there is no Q->energy path.
    with pytest.raises(ValueError):
        units.neutron_units(3.14, "q_inv_angstrom", "energy_meV")


def test_negative_energy_input_rejected():
    # The direct energy_meV input branch must reject negatives like the k/wavelength branches.
    with pytest.raises(ValueError):
        units.neutron_units(-1, "energy_meV", "k_inv_angstrom")
