"""Cooper-Nathans energy resolution (isar.synth.resolution) on a committed IN8 numor copy.

The width is computed from the numor header alone (the instrument parameters), so the artificial
copy (`artificial_data/ILL IN8/055182`: header kept as the facility wrote it, data-row numbers
replaced) gives the same width as the real numor. That was checked when the copy was made: the real
055182 and the copy both gave 0.5795977170638044 meV, the Phase-1 value (docs/in8_phase1_check.py,
~0.58 meV for this DIR-397 configuration, a port of ResCal `rc_cnmat`, Cooper & Nathans 1967). This
test pins the promoted function (isar/synth/resolution) to that header-derived value and exercises
the no-triangle fallback.
"""
import pytest

from artificial import IN8_ANCHOR
from isar.parsers import ill_in8
from isar.synth import resolution

# Header-derived (KFIX, DM, DA, FX, collimations, mosaics, lattice): allowed in code.
CN_FWHM_055182 = 0.5795977170638044


def _record():
    card = ill_in8.card_template()
    return ill_in8.parse_file(IN8_ANCHOR, card)[0]


def test_cn_energy_fwhm_recovers_elastic_width_on_055182():
    fwhm = resolution.cn_energy_fwhm(_record(), w=0.0)
    assert fwhm == pytest.approx(CN_FWHM_055182, rel=1e-9)
    assert 0.45 < fwhm < 0.75  # Phase-1: ~0.58 meV at the elastic line


def test_cn_energy_fwhm_falls_back_when_triangle_cannot_close():
    rec = _record()
    rec.raw_header["params"]["KFIX"] = 0.001  # too small to close the triangle at this |Q|
    assert resolution.cn_energy_fwhm(rec, w=0.0, fallback=1.23) == 1.23


def test_in8_card_enables_a_quasielastic_component():
    """The IN8 card declares the broad quasi-elastic width (§8), which is what makes
    enumerate_structures admit the QE hypothesis on TAS; S30's card leaves it unset."""
    card = ill_in8.card_template()
    assert card.policy.quasielastic_fwhm == 1.5
